"""record a Claude Code session's transcript to the trails service.

Claude Code writes one jsonl *segment* per session id under the project's
transcripts dir. One *trail* is the recording of one segment: the daemon adopts
a transcript, appends each newly completed line, and ends the trail on segment
transition or shutdown. A later lifetime over the same segment attaches to the
trail already recording it and appends from where that one stopped, so restarts
leave a linear conversation linear; only a history copy opens a trail of its own.

The daemon decides none of that. It reports what it can see — the segment name,
its lines' record uuids and digests, and the sibling segment files sharing those
records — and blazes with that evidence; the store's harness resolver answers
with the trail recording from here, the ordinal to append from, and the line
ranges that trail owns, or declines a transcript claude has not finished writing
(`bro/trails/claude_lineage.py`). The daemon then uploads those ranges and keeps
appending to the last one.

The append endpoint classifies records and folds usage, turns, harness version,
and claude's generated title into the header. Quiet ticks keep the trail alive
for the server's lost-sweep. The current trail id is published to the session's
trail pointer (`monitor/trail_pointer.py`) for summon provenance.

The daemon is started by the in-place session runner (`ride/ride/claude/recorder.py`) next to
claude for every session flavor and finalizes on SIGTERM — one last append,
then `end` with `ok`, or `raised` plus the reason when the transcript's
terminal record stream carries a bro `raise` service-tool call.
"""

import enum
import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple, Optional

from bro.base import configs, credentials, log
from bro.base.args import Parser
from bro.launch.hold import session_hold
from bro.monitor import health, trail_pointer, working_projects_dir
from bro.summon import summoned_by_from_env
from bro.trails.backends import CLAUDE_ADAPTER
from bro.trails.model import BlazeRequest, payload_sha256
from bro.trails.record.spine import Recording
from bro.trails.rows import project_messages
from bro.trails.store import TrailsStore, default_store

# the bro service `raise` tool's wire name in a claude session's transcript
_RAISE_TOOL = 'mcp__bro__raise'

# one line of lineage evidence: the record's uuid when it carries one, and the
# digest the stored row would hold
_EvidenceLine = list[Optional[str]]


class _Signature(NamedTuple):
  """a segment file's identity for one tick; an unchanged one means nothing was
  written to the file since it was taken."""

  segment: str
  modified_ns: int
  size: int


class _Position(NamedTuple):
  """how far a segment file has been consumed, and the identity it carried at
  that read — the pair is what makes `byte_extent` trustworthy."""

  signature: _Signature
  byte_extent: int


class _Progress(enum.Enum):
  """what one pass over the recorded segment found."""

  QUIET = enum.auto()
  ADVANCED = enum.auto()
  REWOUND = enum.auto()


def _user_content_has_text(content: Any) -> bool:
  if isinstance(content, str):
    return True
  if not isinstance(content, list):
    return False
  return any(
    isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
    for block in content
  )


def _projected_messages(records: list[str], offset: int) -> list[dict]:
  """the tool calls and user inputs `records` carry, projected the way the store
  projects the rows they become. a recording token holds no read permission, so
  a trail's own writer derives what it needs from what it wrote."""
  return project_messages(
    CLAUDE_ADAPTER,
    [{'step_id': offset + index, 'body': record} for index, record in enumerate(records)],
    {'tool_call', 'user_input'},
  )


def _fold_raise_reason(raised: Optional[str], messages: Iterable[dict]) -> Optional[str]:
  """carry the terminal raise reason across a batch: a `raise` call sets it, and
  anything the human types afterwards clears it again."""
  for message in messages:
    if message.get('type') == 'tool_call' and message.get('tool_name') == _RAISE_TOOL:
      arguments = message.get('arguments')
      reason = arguments.get('reason') if isinstance(arguments, dict) else None
      raised = reason if isinstance(reason, str) else ''
    elif (
      raised is not None
      and message.get('type') == 'user_input'
      and message.get('isMeta') is not True
      and message.get('interrupted') is not True
      and _user_content_has_text(message.get('content'))
    ):
      raised = None
  return raised


def _read_lines_after(path: Path, byte_offset: int) -> tuple[list[str], int]:
  with path.open('rb') as stream:
    stream.seek(byte_offset)
    payload = stream.read()
  complete_size = payload.rfind(b'\n') + 1
  if complete_size == 0:
    return [], byte_offset
  lines = payload[:complete_size].decode('utf-8').split('\n')[:-1]
  return lines, byte_offset + complete_size


def _record_uuid(raw: str) -> Optional[str]:
  entry = CLAUDE_ADAPTER.parse(raw).native['record']
  uuid = entry.get('uuid') if isinstance(entry, dict) else None
  return uuid if isinstance(uuid, str) else None


def _carries_record(path: Path, uuid: str) -> bool:
  """whether the transcript at `path` holds the record `uuid`, reading no further
  than the line that does. the raw line is tested for the uuid before it is
  parsed, so the only lines that cost a parse are the ones naming it — its own,
  and whichever record claims it as a parent."""
  needle = uuid.encode()
  with path.open('rb') as stream:
    return any(
      needle in raw and raw.endswith(b'\n') and _record_uuid(raw.decode()) == uuid for raw in stream
    )


def _evidence_lines(file_lines: list[str]) -> list[_EvidenceLine]:
  return [[_record_uuid(line), payload_sha256(line)] for line in file_lines]


def _signature(path: Path) -> Optional[_Signature]:
  try:
    stat = path.stat()
  except FileNotFoundError:
    return None
  return _Signature(path.stem, stat.st_mtime_ns, stat.st_size)


def _modified_at(path: Path) -> float:
  try:
    return path.stat().st_mtime
  except FileNotFoundError:
    return 0.0


def _compose(file_lines: list[str], chunks: list[list[int]]) -> list[str]:
  lines: list[str] = []
  for start, end in chunks:
    lines.extend(file_lines[start:end])
  return lines


def _launch_context() -> Optional[Any]:
  raw = os.environ.get('RIDE_SESSION_CONTEXT')
  if raw is None:
    return None
  try:
    return json.loads(raw)
  except json.JSONDecodeError as e:
    log.warning('unparsable RIDE_SESSION_CONTEXT (%s); omitting the launch context', e)
    return None


def _in_container() -> bool:
  return os.path.isfile('/.dockerenv')


def _location(workspace: str) -> dict:
  in_container = _in_container()
  location: dict[str, Any] = {'workspace': workspace, 'is_container': in_container}
  # `host` is the host machine, never a container hostname: the launcher stamps
  # RIDE_HOST into the container env (workspace/docker.py); on host we are it
  host = os.environ.get('RIDE_HOST')
  if host is None and not in_container:
    host = socket.gethostname()
  if host is not None:
    location['host'] = host
  directory = os.environ.get('RIDE_HOST_WORKSPACE') if in_container else str(Path.cwd())
  if directory is not None:
    location['dir'] = directory
  return location


def _workspace_name() -> Optional[str]:
  return os.environ.get('RIDE_WORKSPACE')


def _verdict_chunks(result: dict) -> list[list[int]]:
  chunks = result.get('chunks')
  if (
    not isinstance(chunks, list)
    or len(chunks) == 0
    or not all(
      isinstance(chunk, list)
      and len(chunk) == 2
      and all(isinstance(bound, int) and not isinstance(bound, bool) for bound in chunk)
      for chunk in chunks
    )
  ):
    raise ValueError(f'lineage verdict carries malformed chunks: {chunks!r}')
  return [list(chunk) for chunk in chunks]


class _SegmentRecorder:
  """records one segment file into one trail: the lines the blaze verdict
  awarded the trail, then every line the file grows by."""

  def __init__(
    self,
    store: TrailsStore,
    path: Path,
    trail_id: str,
    *,
    extent: int,
    pending: list[str],
    position: _Position,
  ) -> None:
    """`extent` is what the trail already holds, `pending` the awarded lines and
    `position` the read they came from; the trail owns everything the file
    carries past it."""
    self.path = path
    self.trail_id = trail_id
    self._recording = Recording(store, trail_id, extent)
    self._pending = pending
    self._position = position
    self._raised: Optional[str] = None

  @property
  def segment(self) -> str:
    return self.path.stem

  def tick(self) -> _Progress:
    """record what the trail is owed — the awarded lines, then whatever the
    segment has grown by."""
    signature = _signature(self.path)
    if signature is None:
      self._recording.keepalive_if_idle()
      return _Progress.QUIET
    if signature.size < self._position.byte_extent:
      return _Progress.REWOUND
    records, byte_extent = self._pending, self._position.byte_extent
    if signature != self._position.signature:
      grown, byte_extent = _read_lines_after(self.path, byte_extent)
      records = [*records, *grown]
    if len(records) == 0:
      self._position = _Position(signature, byte_extent)
      self._recording.keepalive_if_idle()
      return _Progress.QUIET
    offset = self._recording.append(records)
    self._raised = _fold_raise_reason(self._raised, _projected_messages(records, offset))
    self._pending = []
    self._position = _Position(signature, byte_extent)
    log.info(
      'recorded trail %s (%d lines, segment %s)',
      self.trail_id,
      self._recording.extent,
      self.segment[:12],
    )
    return _Progress.ADVANCED

  def is_quiet(self) -> bool:
    """True when the segment has not been written since the last read."""
    signature = _signature(self.path)
    return signature is None or signature == self._position.signature

  def close(self) -> None:
    """final append, then end the trail — `ok`, or `raised` with the reason
    when the recorded stream's terminal state is a raise call."""
    self.tick()
    if self._raised is not None:
      detail = self._raised if len(self._raised) > 0 else 'raise reason unavailable'
      self._recording.end('raised', detail=detail)
    else:
      self._recording.end('ok')
    log.info('trail %s ended (%s)', self.trail_id, 'raised' if self._raised is not None else 'ok')


class Recorder:
  """supervises one workspace's transcripts: which segment file is the session's
  transcript right now, what lineage evidence its trail is blazed from, and when
  the segment it handed to a `_SegmentRecorder` is over. `started_after` gates
  segment adoption to files modified after the session launched, so a reused
  workspace's older transcripts are not re-recorded."""

  def __init__(
    self,
    projects_dir: Path,
    workspace: str,
    client: TrailsStore,
    *,
    llm: dict,
    ride_command: str,
    started_after: float,
  ) -> None:
    self.projects_dir = projects_dir
    self.workspace = workspace
    self.client = client
    self.llm = llm
    self.ride_command = ride_command
    self.started_after = started_after
    # read once: every segment of this recorder lifetime belongs to the same
    # summoned run, and the reader consumes the env var
    self.summoned_by = summoned_by_from_env()
    self._active: Optional[_SegmentRecorder] = None
    self._consumed: set[str] = set()
    self._declined_signature: Optional[_Signature] = None
    # a stale pointer from a previous lifetime must not attribute this
    # session's summons to an ended trail
    trail_pointer.clear()

  # --- tick loop -----------------------------------------------------------------

  def tick(self) -> bool:
    """one pass; True when a server write advanced the trail."""
    active = self._active
    if active is None:
      return self._maybe_adopt()
    candidate = self._pick_segment()
    if candidate is not None and candidate.stem != active.segment:
      return self._maybe_transition(active, candidate)
    return self._advance(active)

  def finalize(self) -> bool:
    """the daemon's shutdown pass: one last append, then end the trail."""
    if self._active is None:
      self._maybe_adopt()
    if self._active is None:
      return False
    self._close(self._active)
    return True

  def _advance(self, active: _SegmentRecorder) -> bool:
    progress = active.tick()
    if progress is not _Progress.REWOUND:
      return progress is _Progress.ADVANCED
    log.warning('segment %s shrank below trail %s; closing', active.segment[:12], active.trail_id)
    self._close(active)
    return True

  # --- segment selection ----------------------------------------------------------

  def _pick_segment(self) -> Optional[Path]:
    if not self.projects_dir.is_dir():
      return None
    active = self._active.segment if self._active is not None else None
    best: Optional[Path] = None
    best_mtime = 0.0
    for path in self.projects_dir.iterdir():
      if path.suffix != '.jsonl' or path.stem in self._consumed:
        continue
      try:
        mtime = path.stat().st_mtime
      except FileNotFoundError:
        continue
      if path.stem != active and mtime < self.started_after:
        continue
      if mtime > best_mtime:
        best = path
        best_mtime = mtime
    return best

  # --- adoption -------------------------------------------------------------------

  def _maybe_adopt(self) -> bool:
    path = self._pick_segment()
    if path is None:
      return False
    signature = _signature(path)
    if signature is None or signature == self._declined_signature:
      # unchanged since the resolver declined it; ask again when it grows
      return False
    try:
      file_lines, byte_extent = _read_lines_after(path, 0)
    except OSError:
      return False
    lines = _evidence_lines(file_lines)
    if not any(uuid is not None for uuid, _ in lines):
      # nothing but head ephemera: a history copy may still land, and the
      # segment's lineage is settled by the tick that adopts it
      return False
    lineage = {
      'segment': path.stem,
      'lines': lines,
      'related_segments': self._related_segments(path, lines),
    }
    verdict = self._blaze(path.stem, lineage)
    if verdict is None:
      self._declined_signature = signature
      return False
    chunks = _verdict_chunks(verdict)
    awarded = _compose(file_lines, [*chunks[:-1], [chunks[-1][0], len(file_lines)]])
    active = _SegmentRecorder(
      self.client,
      path,
      verdict['id'],
      extent=verdict['extent'],
      pending=awarded,
      position=_Position(signature, byte_extent),
    )
    self._active = active
    trail_pointer.publish(active.trail_id)
    active.tick()
    return True

  def _related_segments(self, path: Path, lines: list[_EvidenceLine]) -> list[str]:
    """the segments whose files carry this transcript's first record: claude
    rewrites `sessionId` in a history copy, so a record uuid is the only link
    back to where the conversation was, and each copy in the chain carries it."""
    first = next((uuid for uuid, _ in lines if uuid is not None), None)
    if first is None:
      return []
    related: list[str] = []
    for other in sorted(self.projects_dir.glob('*.jsonl'), key=_modified_at, reverse=True):
      if other.stem == path.stem:
        continue
      try:
        carries = _carries_record(other, first)
      except OSError:
        continue
      if carries:
        related.append(other.stem)
    return related

  # --- trail lifecycle ------------------------------------------------------------

  def _blaze(self, segment: str, lineage: dict) -> Optional[dict]:
    """settle the trail this transcript records into and return the verdict;
    None when the resolver declines to adopt the segment yet."""
    body: dict[str, Any] = {'records': []}
    context = _launch_context()
    if context is not None:
      body['launch_context'] = context
    request = BlazeRequest(
      harness='claude',
      version=configs.VERSION,
      interactive=True,
      surface='ride',
      native={
        'llm': self.llm,
        'segment': segment,
        'ride_command': self.ride_command,
        'harness_version': 'unknown',
      },
      location=_location(self.workspace),
      body=body,
      bro=os.environ.get('RIDE_BRO'),
      hold=session_hold(),
      summoned_by=self.summoned_by,
      lineage=lineage,
    )
    result = self.client.blaze(request)
    if result.get('adopted') is False:
      log.info('segment %s not adopted yet (%s)', segment[:12], result.get('reason'))
      return None
    trail_id = result['id']
    if result.get('attached') is True:
      log.info(
        'trail %s continues segment %s from step %d',
        trail_id,
        segment[:12],
        result['extent'],
      )
      return result
    forked_from = result.get('forked_from')
    if forked_from is None:
      log.info(
        'trail %s opens at segment %s (root: %s)', trail_id, segment[:12], result.get('reason')
      )
    else:
      log.info(
        'trail %s opens at segment %s (forked from %s @ %s)',
        trail_id,
        segment[:12],
        forked_from['trail_id'],
        forked_from['step_id'],
      )
    return result

  def _maybe_transition(self, active: _SegmentRecorder, new_path: Path) -> bool:
    if not self._active_is_quiet(active):
      # a live active segment plus an unrelated newer jsonl: hold rather than
      # flip recording back and forth between two growing files
      log.warning(
        'segment %s appeared while %s is still growing; holding',
        new_path.stem[:12],
        active.segment[:12],
      )
      return self._advance(active)
    self._close(active)
    self._maybe_adopt()
    return True

  def _active_is_quiet(self, active: _SegmentRecorder) -> bool:
    return _modified_at(active.path) < self.started_after or active.is_quiet()

  def _close(self, active: _SegmentRecorder) -> None:
    """end the segment's trail and retire it: nothing adopts it again."""
    active.close()
    self._consumed.add(active.segment)
    self._active = None
    self._declined_signature = None
    trail_pointer.clear()


def _exception_summary(exception: BaseException) -> str:
  return f'{type(exception).__name__}: {exception}'


def _attempt(step: Callable[[], bool], *, interval: Optional[int]) -> None:
  """run one recorder pass and beat the health file with its outcome, quiet
  passes included — a beat that stops arriving is how a killed daemon is seen."""
  try:
    step()
  except Exception as e:
    log.exception('recording failed')
    health.write('error', _exception_summary(e), interval=interval)
    return
  health.write('ok', interval=interval)


def _watch(recorder: Recorder, interval: int) -> None:
  stop = threading.Event()
  parent_pid = os.getppid()

  def _handle_signal(signum, frame):
    del signum, frame
    stop.set()

  signal.signal(signal.SIGTERM, _handle_signal)
  signal.signal(signal.SIGINT, _handle_signal)

  while not stop.is_set():
    if os.getppid() != parent_pid:
      log.info('parent process exited, shutting down')
      break
    _attempt(recorder.tick, interval=interval)
    stop.wait(interval)

  _attempt(recorder.finalize, interval=None)


def record_session(
  interval: int = 3,
  workspace: Optional[str] = None,
  projects_dir: Optional[Path] = None,
  llm: Optional[str] = None,
) -> int:
  workspace_name = workspace if workspace is not None else _workspace_name()
  if workspace_name is None:
    log.error('cannot determine workspace name; pass --workspace or set RIDE_WORKSPACE')
    return 1
  ride_command = os.environ.get('RIDE_COMMAND')
  if ride_command is None:
    log.error('RIDE_COMMAND is not set; the trail header requires the launch command')
    return 1
  try:
    llm_recipe = json.loads(llm) if llm is not None else {}
  except json.JSONDecodeError as e:
    log.error('invalid --llm json: %s', e)
    return 1

  try:
    client = default_store()
  except credentials.SecretNotFound:
    material_path = credentials.default_store().material_path('trails')
    log.error(
      'config not found: trails (expected %s; see %s)',
      material_path,
      credentials.CREDENTIAL_MIGRATION_GUIDE,
    )
    health.write('error', 'config not found: trails', interval=None)
    return 1

  src = projects_dir if projects_dir is not None else working_projects_dir()
  recorder = Recorder(
    src,
    workspace_name,
    client,
    llm=llm_recipe,
    ride_command=ride_command,
    started_after=time.time(),
  )
  log.info('recording %s (interval=%ds, workspace=%s)', src, interval, workspace_name)
  _watch(recorder, interval)
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='record a Claude Code session transcript to trails')
  parser.add_argument(
    '--interval', type=int, default=3, help='poll interval in seconds (default: 3)'
  )
  parser.add_argument(
    '--workspace', default=None, help='workspace name (default: from RIDE_WORKSPACE)'
  )
  parser.add_argument(
    '--projects-dir',
    type=Path,
    default=None,
    help='claude projects dir to record (default: derived from the config dir and cwd)',
  )
  parser.add_argument(
    '--llm',
    default=None,
    help='launch recipe json for native.llm, e.g. {"model": "...", "effort": "..."}',
  )
  return record_session(**parser.parse(argv))
