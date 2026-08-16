"""record a Claude Code session's transcript to the trails service.

Claude Code writes one jsonl *segment* per session id under the project's
transcripts dir. One *trail* is the suffix recorded during one recorder
lifetime within one segment: the daemon adopts a transcript, appends each newly
completed line, and ends the trail on segment transition or shutdown. Every
process resume therefore opens a new trail, and a verified continuation is
recorded as a fork.

The daemon does not decide those forks. It reports what it can see — the segment
name, its lines' record uuids and digests, and the sibling segment files sharing
those records — and blazes with that evidence; the store's harness resolver
answers with the fork point and the line ranges the new trail owns, or declines
a transcript claude has not finished writing (`bro/trails/claude_lineage.py`).
The daemon then uploads those ranges and keeps appending to the last one.

The append endpoint classifies records and folds usage, turns, harness version,
and claude's generated title into the header. Quiet ticks keep the trail alive
for the server's lost-sweep. The current trail id is published to the session's
trail pointer (`monitor/trail_pointer.py`) for summon provenance.

The daemon is started by the in-place session runner (`cw/recorder.py`) next to
claude for every session flavor and finalizes on SIGTERM — one last append,
then `end` with `ok`, or `raised` plus the reason when the transcript's
terminal record stream carries a bro `raise` service-tool call.
"""

import dataclasses
import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Optional

from bro.base import configs, credentials, log
from bro.base.args import Parser
from bro.monitor import health, trail_pointer, working_projects_dir
from bro.trails.backends import CLAUDE_ADAPTER
from bro.trails.model import BlazeRequest, payload_sha256
from bro.trails.record.spine import Recording
from bro.trails.store import TrailsStore, default_store

# the bro service `raise` tool's wire name in a claude session's transcript
_RAISE_TOOL = 'mcp__bro__raise'

# one line of lineage evidence: the record's uuid when it carries one, and the
# digest the stored row would hold
_EvidenceLine = list[Optional[str]]

# a segment file's identity for one tick: name, modification time, size
_Signature = tuple[str, int, int]


def _user_content_has_text(content: Any) -> bool:
  if isinstance(content, str):
    return True
  if not isinstance(content, list):
    return False
  return any(
    isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
    for block in content
  )


def _terminal_raise_reason(messages: Iterator[dict]) -> Optional[str]:
  raised: Optional[str] = None
  for message in messages:
    if message.get('type') == 'tool_call' and message.get('tool_name') == _RAISE_TOOL:
      arguments = message.get('arguments')
      reason = arguments.get('reason') if isinstance(arguments, dict) else None
      raised = reason if isinstance(reason, str) else ''
    elif (
      raised is not None
      and message.get('type') == 'user_input'
      and message.get('isMeta') is not True
      and _user_content_has_text(message.get('content'))
    ):
      raised = None
  return raised


def _read_lines(path: Path) -> list[str]:
  lines, _ = _read_lines_after(path, 0)
  return lines


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


def _evidence_lines(file_lines: list[str]) -> list[_EvidenceLine]:
  return [[_record_uuid(line), payload_sha256(line)] for line in file_lines]


@dataclasses.dataclass
class _Stream:
  """the segment line ranges the active trail holds — at most a pre-copy head
  plus the tail, each `[start, end)`. The final range's end is the consumed line
  extent as uploaded; `line_count` is how many rows the server holds."""

  trail_id: str
  segment: str
  chunks: list[list[int]]

  @property
  def extent(self) -> int:
    return self.chunks[-1][1]

  @property
  def line_count(self) -> int:
    return sum(end - start for start, end in self.chunks)


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
  raw = os.environ.get('CW_SESSION_CONTEXT')
  if raw is None:
    return None
  try:
    return json.loads(raw)
  except json.JSONDecodeError as e:
    log.warning('unparsable CW_SESSION_CONTEXT (%s); omitting the launch context', e)
    return None


def _in_container() -> bool:
  return os.path.isfile('/.dockerenv')


def _location(workspace: str) -> dict:
  in_container = _in_container()
  location: dict[str, Any] = {'workspace': workspace, 'is_container': in_container}
  # `host` is the host machine, never a container hostname: the launcher stamps
  # CW_HOST into the container env (workspace/docker.py); on host we are it
  host = os.environ.get('CW_HOST')
  if host is None and not in_container:
    host = socket.gethostname()
  if host is not None:
    location['host'] = host
  directory = os.environ.get('CW_HOST_WORKSPACE') if in_container else str(Path.cwd())
  if directory is not None:
    location['dir'] = directory
  return location


def _workspace_name() -> Optional[str]:
  return os.environ.get('CW_NAME')


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


class Recorder:
  """tracks one workspace's active transcript and records it. `started_after`
  gates segment adoption to files modified after the session launched, so a
  reused workspace's older transcripts are not re-recorded."""

  def __init__(
    self,
    projects_dir: Path,
    workspace: str,
    client: TrailsStore,
    *,
    llm: dict,
    cw_command: str,
    started_after: float,
  ) -> None:
    self.projects_dir = projects_dir
    self.workspace = workspace
    self.client = client
    self.llm = llm
    self.cw_command = cw_command
    self.started_after = started_after
    self.active: Optional[_Stream] = None
    self._recording: Optional[Recording] = None
    self._consumed: set[str] = set()
    self._recorded_signature: Optional[_Signature] = None
    self._active_signature: Optional[_Signature] = None
    self._declined_signature: Optional[_Signature] = None
    self._active_byte_extent = 0
    # a stale pointer from a previous lifetime must not attribute this
    # session's summons to an ended trail
    trail_pointer.clear()

  # --- tick loop -----------------------------------------------------------------

  def tick(self) -> bool:
    """one pass; True when a server write advanced the trail."""
    if self.active is None:
      return self._maybe_adopt()
    candidate = self._pick_segment()
    if candidate is not None and candidate.stem != self.active.segment:
      return self._maybe_transition(candidate)
    return self._append_if_changed()

  def finalize(self) -> bool:
    """the daemon's shutdown pass: one last append, then end the trail."""
    if self.active is None:
      self._maybe_adopt()
    if self.active is None:
      return False
    self._close_active()
    return True

  # --- segment selection ----------------------------------------------------------

  def _pick_segment(self) -> Optional[Path]:
    if not self.projects_dir.is_dir():
      return None
    active = self.active.segment if self.active is not None else None
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

  @staticmethod
  def _signature(path: Path) -> Optional[_Signature]:
    try:
      stat = path.stat()
    except FileNotFoundError:
      return None
    return (path.stem, stat.st_mtime_ns, stat.st_size)

  # --- adoption -------------------------------------------------------------------

  def _maybe_adopt(self) -> bool:
    path = self._pick_segment()
    if path is None:
      return False
    signature = self._signature(path)
    if signature is None or signature == self._declined_signature:
      # unchanged since the resolver declined it; ask again when it grows
      return False
    try:
      file_lines = _read_lines(path)
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
    if not self._blaze(path.stem, lineage):
      self._declined_signature = signature
      return False
    self._append_if_changed()
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
        other_lines = _read_lines(other)
      except OSError:
        continue
      if any(_record_uuid(line) == first for line in other_lines):
        related.append(other.stem)
    return related

  # --- trail lifecycle ------------------------------------------------------------

  def _blaze(self, segment: str, lineage: dict) -> bool:
    """open the trail this transcript continues; False when the resolver
    declines to adopt the segment yet."""
    body: dict[str, Any] = {'records': []}
    context = _launch_context()
    if context is not None:
      body['launch_context'] = context
    request = BlazeRequest(
      harness='claude',
      version=configs.VERSION,
      interactive=True,
      surface='cw',
      native={
        'llm': self.llm,
        'segment': segment,
        'cw_command': self.cw_command,
        'harness_version': 'unknown',
      },
      location=_location(self.workspace),
      body=body,
      bro=os.environ.get('CW_BRO'),
      hold=os.environ.get('BRO_HOLD'),
      lineage=lineage,
    )
    result = self.client.blaze(request)
    if result.get('adopted') is False:
      log.info('segment %s not adopted yet (%s)', segment[:12], result.get('reason'))
      return False
    trail_id = result['id']
    forked_from = result.get('forked_from')
    self.active = _Stream(trail_id=trail_id, segment=segment, chunks=_verdict_chunks(result))
    self._recording = Recording(self.client, trail_id, 0)
    self._recorded_signature = None
    self._active_byte_extent = 0
    trail_pointer.publish(trail_id)
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
    return True

  def _active_path(self) -> Path:
    assert self.active is not None
    return self.projects_dir / (self.active.segment + '.jsonl')

  def _append_if_changed(self) -> bool:
    active = self.active
    assert active is not None
    path = self._active_path()
    signature = self._signature(path)
    if signature is None:
      self._keepalive_if_idle()
      return False
    self._active_signature = signature
    if signature == self._recorded_signature:
      self._keepalive_if_idle()
      return False

    if self._recorded_signature is None:
      file_lines, byte_extent = _read_lines_after(path, 0)
      if len(file_lines) < active.chunks[-1][0]:
        log.warning(
          'segment %s shrank below trail %s; closing', active.segment[:12], active.trail_id
        )
        self._close_active(append=False)
        return True
      chunks = [list(chunk) for chunk in active.chunks]
      chunks[-1][1] = len(file_lines)
      records = _compose(file_lines, chunks)
      offset = 0
    else:
      if signature[2] < self._active_byte_extent:
        log.warning(
          'segment %s shrank below trail %s; closing', active.segment[:12], active.trail_id
        )
        self._close_active(append=False)
        return True
      records, byte_extent = _read_lines_after(path, self._active_byte_extent)
      offset = active.line_count
      chunks = [list(chunk) for chunk in active.chunks]
      chunks[-1][1] += len(records)

    if len(records) == 0:
      self._recorded_signature = signature
      self._keepalive_if_idle()
      return False
    recording = self._recording
    assert recording is not None
    if recording.extent != offset:
      raise RuntimeError(
        f'local extent for trail {active.trail_id} is {recording.extent}, expected {offset}'
      )
    recording.append(records)
    active.chunks = chunks
    self._active_byte_extent = byte_extent
    self._recorded_signature = signature
    log.info(
      'recorded trail %s (%d lines, segment %s)',
      active.trail_id,
      active.line_count,
      active.segment[:12],
    )
    return True

  def _keepalive_if_idle(self) -> None:
    if self._recording is not None:
      self._recording.keepalive_if_idle()

  def _maybe_transition(self, new_path: Path) -> bool:
    active = self.active
    assert active is not None
    if not self._active_is_quiet():
      # a live active segment plus an unrelated newer jsonl: hold rather than
      # flip recording back and forth between two growing files
      log.warning(
        'segment %s appeared while %s is still growing; holding',
        new_path.stem[:12],
        active.segment[:12],
      )
      return self._append_if_changed()
    self._close_active()
    self._maybe_adopt()
    return True

  def _active_is_quiet(self) -> bool:
    signature = self._signature(self._active_path())
    if signature is None:
      return True
    try:
      if self._active_path().stat().st_mtime < self.started_after:
        return True
    except FileNotFoundError:
      return True
    return signature == self._active_signature

  def _close_active(self, *, append: bool = True) -> None:
    """final append, then end the trail — `ok`, or `raised` with the reason
    when the recorded stream's terminal state is a raise call."""
    if append:
      self._append_if_changed()
      if self.active is None:
        return
    active = self.active
    recording = self._recording
    assert active is not None and recording is not None
    raised = _terminal_raise_reason(
      self.client.iter_messages(active.trail_id, types={'tool_call', 'user_input'})
    )
    if raised is not None:
      detail = raised if len(raised) > 0 else 'raise reason unavailable'
      recording.end('raised', detail=detail)
    else:
      recording.end('ok')
    log.info('trail %s ended (%s)', active.trail_id, 'raised' if raised is not None else 'ok')
    self._consumed.add(active.segment)
    self.active = None
    self._recording = None
    self._recorded_signature = None
    self._declined_signature = None
    self._active_byte_extent = 0
    trail_pointer.clear()


def _exception_summary(exception: BaseException) -> str:
  return f'{type(exception).__name__}: {exception}'


def _attempt(step: Callable[[], bool]) -> None:
  try:
    advanced = step()
  except Exception as e:
    log.exception('recording failed')
    health.write('error', _exception_summary(e))
    return
  if advanced:
    health.write('ok')


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
    _attempt(recorder.tick)
    stop.wait(interval)

  _attempt(recorder.finalize)


def record_session(
  interval: int = 3,
  workspace: Optional[str] = None,
  projects_dir: Optional[Path] = None,
  llm: Optional[str] = None,
) -> int:
  workspace_name = workspace if workspace is not None else _workspace_name()
  if workspace_name is None:
    log.error('cannot determine workspace name; pass --workspace or set CW_NAME')
    return 1
  cw_command = os.environ.get('CW_COMMAND')
  if cw_command is None:
    log.error('CW_COMMAND is not set; the trail header requires the launch command')
    return 1
  try:
    llm_recipe = json.loads(llm) if llm is not None else {}
  except json.JSONDecodeError as e:
    log.error('invalid --llm json: %s', e)
    return 1

  try:
    client = default_store()
  except credentials.SecretNotFound:
    log.error('config not found: trails (configure ~/.bro/trails.json)')
    health.write('error', 'config not found: trails')
    return 1

  src = projects_dir if projects_dir is not None else working_projects_dir()
  recorder = Recorder(
    src,
    workspace_name,
    client,
    llm=llm_recipe,
    cw_command=cw_command,
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
  parser.add_argument('--workspace', default=None, help='workspace name (default: from CW_NAME)')
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
