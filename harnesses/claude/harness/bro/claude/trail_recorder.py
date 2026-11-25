"""record a Claude Code session's transcripts to trails, one trail per stretch.

Claude Code writes one jsonl transcript per session id under the project's
transcripts dir, and the session writes to one of them at a time. The recorder
follows that one, and each stretch it follows is a trail of its own: a trail
takes every line of the transcript no trail holds, from the first such line on.
Which trail holds which lines of which transcript is the session's recording map
(`bro/trails/record/recording_map.py`), and the recorder decides each trail's
edge from that map and the transcripts alone:

- a transcript the map knows continues its last trail: the new trail starts at
  the line after the last held one, with a `resume` edge to that line's step;
- a transcript the map does not know may copy one it knows, its origin
  (`/branch`, `--fork-session`): the one its first record's
  `forkedFrom.sessionId` names, or else the one carrying the longest run of its
  leading record uuids. Once the copy carries a record of its own, the trail
  takes its head and the lines after the last copied one a trail holds, looked
  up by uuid through the origin and, for the lines the origin copied in turn,
  through the origin's own origin, with a `branch` edge to the step holding it;
- any other transcript, or a copy no trail holds a line of, is a root.

Each trail is started under an id the recorder mints and writes to the map
first, so a start whose reply was lost is answered with the trail it started.
The recorder ends the trail when the session moves to another transcript,
marking its entry ended; a transcript it turns back to is followed by the same
rules. A transcript that shrank below its trail ends that trail and loses its
entries.

The append endpoint classifies records and folds usage, turns, harness version,
and claude's generated title into the header. Quiet ticks keep the trail alive
for the server's lost-sweep. The current trail id is published to the session's
trail pointer (`monitor/trail_pointer.py`) for summon provenance.

The daemon is started by the Claude session runner (`harnesses/claude/harness/bro/claude/recorder.py`) next to
claude for every session flavor and finalizes on SIGTERM — one last append,
then `end` with `ok`, or `raised` plus the reason when the transcript's
terminal record stream carries a bro `raise` service-tool call.
"""

import enum
import json
import os
import signal
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple, Optional

from bro.base import configs, credentials, log
from bro.base.args import Parser
from bro.claude.claude_config import transcript_clock_now
from bro.claude.session_end_state import RAISE_TOOL
from bro.claude.transcripts import read_lines_after, watching
from bro.claude_trails.format import CLAUDE_FORMAT
from bro.launch.hold import session_hold
from bro.monitor import SESSION_DIR_ENV, health, session_dir, trail_pointer
from bro.summon import summoned_by_from_env
from bro.trails.model import StartRequest
from bro.trails.record.recording_map import RecordingMap, RecordingMapError, Span, TrailEntry
from bro.trails.record.session import ManagedSession, managed_session
from bro.trails.record.settling import (
  SettledTrail,
  give_up_record,
  settle_record,
  trail_extent,
  verify_trail,
)
from bro.trails.record.spine import MappedStart, Recording
from bro.trails.rows import project_messages
from bro.trails.store import (
  InvalidRequest,
  PermissionDenied,
  TrailCollision,
  TrailNotFound,
  TrailsStore,
  default_store,
)

_REFUSALS = (InvalidRequest, PermissionDenied, TrailCollision, TrailNotFound, RecordingMapError)


class _Signature(NamedTuple):
  """a transcript file's identity for one tick; an unchanged one means nothing
  was written to the file since it was taken."""

  record_id: str
  modified_ns: int
  size: int


class _Position(NamedTuple):
  """how far a transcript file has been consumed, and the identity it carried at
  that read — the pair is what makes `byte_extent` trustworthy."""

  signature: _Signature
  byte_extent: int


class _Progress(enum.Enum):
  """what one pass over the recorded transcript found."""

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
    CLAUDE_FORMAT,
    [{'step_id': offset + index, 'body': record} for index, record in enumerate(records)],
    {'tool_call', 'user_input'},
  )


def _fold_raise_reason(raised: Optional[str], messages: Iterable[dict]) -> Optional[str]:
  """carry the terminal raise reason across a batch: a `raise` call sets it, and
  anything the human types afterwards clears it again."""
  for message in messages:
    if message.get('type') == 'tool_call' and message.get('tool_name') == RAISE_TOOL:
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


def _record(raw: str) -> dict:
  entry = CLAUDE_FORMAT.parse(raw).native['record']
  return entry if isinstance(entry, dict) else {}


def _record_uuid(raw: str) -> Optional[str]:
  uuid = _record(raw).get('uuid')
  return uuid if isinstance(uuid, str) else None


@dataclass(frozen=True)
class _Transcript:
  """a transcript's complete lines as one read found them, the record uuid each
  carries, and the first line carrying each uuid."""

  lines: list[str]
  uuids: list[Optional[str]]
  line_by_uuid: dict[str, int]

  @classmethod
  def read(cls, path: Path) -> '_Transcript':
    lines, _ = read_lines_after(path, 0)
    uuids = [_record_uuid(line) for line in lines]
    line_by_uuid: dict[str, int] = {}
    for line, uuid in enumerate(uuids):
      if uuid is not None:
        line_by_uuid.setdefault(uuid, line)
    return cls(lines, uuids, line_by_uuid)

  def first_record(self) -> Optional[int]:
    return next((line for line, uuid in enumerate(self.uuids) if uuid is not None), None)


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


def _compose(lines: list[str], spans: Iterable[Span]) -> list[str]:
  """the lines `spans` hold, in step order."""
  composed: list[str] = []
  for span in spans:
    composed.extend(lines[span.start : span.end])
  return composed


@dataclass(frozen=True)
class _Plan:
  """the trail a stretch starts: the spans it takes, its edge, and the extent at
  which the open span of the record's last trail closes."""

  spans: tuple[Span, ...]
  edge: Optional[dict[str, Any]] = None
  closing: Optional[int] = None


_ROOT = _Plan((Span(0),))


class _Planner:
  """one pass deciding the trail a transcript's next stretch starts, reading
  each transcript, settling each record, and probing and verifying each trail at
  most once."""

  def __init__(self, recording_map: RecordingMap, store: TrailsStore, projects_dir: Path) -> None:
    self._map = recording_map
    self._store = store
    self._projects_dir = projects_dir
    self._transcripts: dict[str, _Transcript] = {}
    self._settled: dict[str, Optional[SettledTrail]] = {}
    self._extents: dict[str, int] = {}
    self._verified: dict[str, bool] = {}

  def plan(self, record_id: str) -> Optional[_Plan]:
    """the trail to start for `record_id`, or None while it holds nothing to
    start one from."""
    transcript = self._transcript(record_id)
    settled = self._settle(record_id)
    if settled is not None:
      first = settled.entry.item_at(settled.extent - 1) + 1
      if len(transcript.lines) <= first:
        return None
      return _Plan(
        (Span(first),),
        {'trail_id': settled.entry.id, 'step_id': settled.extent - 1, 'kind': 'resume'},
        settled.extent if settled.entry.spans[-1].end is None else None,
      )
    first_record = transcript.first_record()
    if first_record is None:
      return None
    origin = self._origin(transcript, first_record)
    if origin is None:
      return _ROOT
    carried = self._transcript(origin).line_by_uuid
    first_own = next(
      (
        line
        for line in range(first_record, len(transcript.lines))
        if transcript.uuids[line] is not None and transcript.uuids[line] not in carried
      ),
      None,
    )
    if first_own is None:
      # the copy may still be being written
      return None
    for line in range(first_own - 1, first_record - 1, -1):
      uuid = transcript.uuids[line]
      holder = None if uuid is None else self._holder(origin, uuid, frozenset())
      if holder is not None:
        trail_id, step = holder
        spans = (Span(line + 1),)
        if first_record > 0:
          spans = (Span(0, first_record), *spans)
        return _Plan(spans, {'trail_id': trail_id, 'step_id': step, 'kind': 'branch'})
    return _ROOT

  def _path(self, record_id: str) -> Path:
    return self._projects_dir / f'{record_id}.jsonl'

  def _transcript(self, record_id: str) -> _Transcript:
    if record_id not in self._transcripts:
      self._transcripts[record_id] = _Transcript.read(self._path(record_id))
    return self._transcripts[record_id]

  def _settle(self, record_id: str) -> Optional[SettledTrail]:
    if record_id not in self._settled:
      settled = settle_record(self._map, record_id, self._store, self._transcript(record_id).lines)
      self._settled[record_id] = settled
      if settled is not None:
        self._extents[settled.entry.id] = settled.extent
        self._verified[settled.entry.id] = True
    return self._settled[record_id]

  def _extent(self, trail_id: str) -> int:
    if trail_id not in self._extents:
      try:
        self._extents[trail_id] = trail_extent(self._store, trail_id)
      except TrailNotFound:
        self._extents[trail_id] = 0
    return self._extents[trail_id]

  def _verify(self, record_id: str, entry: TrailEntry) -> bool:
    if entry.id not in self._verified:
      self._verified[entry.id] = verify_trail(
        self._store, entry, self._extent(entry.id), self._transcript(record_id).lines
      )
    return self._verified[entry.id]

  def _origin(self, transcript: _Transcript, first: int) -> Optional[str]:
    """the known record a transcript the map does not know copies."""
    known = [record_id for record_id in self._map.records if self._path(record_id).exists()]
    forked = _record(transcript.lines[first]).get('forkedFrom')
    named = forked.get('sessionId') if isinstance(forked, dict) else None
    if named in known:
      return named
    leading = [uuid for uuid in transcript.uuids if uuid is not None]
    origin, longest = None, 0
    for candidate in known:
      carried = self._transcript(candidate).line_by_uuid
      run = 0
      while run < len(leading) and leading[run] in carried:
        run += 1
      if run > longest:
        origin, longest = candidate, run
    return origin

  def _holder(
    self, record_id: str, uuid: str, visited: frozenset[str]
  ) -> Optional[tuple[str, int]]:
    """the trail and step holding the line of `record_id` that carries `uuid`,
    through the record it copied that line from."""
    if record_id in visited:
      raise RecordingMapError(f'the trail edges of record {record_id} form a cycle')
    line = self._transcript(record_id).line_by_uuid.get(uuid)
    if line is None:
      return None
    self._settle(record_id)
    entries = self._map.records.get(record_id, [])
    for entry in entries:
      step = entry.step_at(line)
      if step is None:
        continue
      if step >= self._extent(entry.id):
        return None
      if not self._verify(record_id, entry):
        give_up_record(self._map, record_id)
        return None
      return (entry.id, step)
    if len(entries) == 0 or line >= entries[0].spans[-1].start or entries[0].forked_from is None:
      return None
    parent_trail = entries[0].forked_from['trail_id']
    parent = next(
      (
        candidate
        for candidate, candidate_entries in self._map.records.items()
        if any(entry.id == parent_trail for entry in candidate_entries)
      ),
      None,
    )
    if parent is None or not self._path(parent).exists():
      return None
    return self._holder(parent, uuid, visited | {record_id})


class _StretchRecorder:
  """records one stretch of a transcript into one trail: the lines its spans
  hold when it starts, then every line the file grows by."""

  def __init__(
    self,
    path: Path,
    recording: Recording,
    *,
    pending: list[str],
    position: _Position,
  ) -> None:
    """`pending` is what the trail is owed from the read at `position`; the
    trail owns everything the file carries past it."""
    self.path = path
    self._recording = recording
    self._pending = pending
    self._position = position
    self._raised: Optional[str] = None

  @property
  def record_id(self) -> str:
    return self.path.stem

  @property
  def trail_id(self) -> str:
    return self._recording.trail_id

  def tick(self) -> _Progress:
    """record what the trail is owed — the pending lines, then whatever the
    transcript has grown by."""
    signature = _signature(self.path)
    if signature is None:
      self._recording.keepalive_if_idle()
      return _Progress.QUIET
    if signature.size < self._position.byte_extent:
      return _Progress.REWOUND
    records, byte_extent = self._pending, self._position.byte_extent
    if signature != self._position.signature:
      grown, byte_extent = read_lines_after(self.path, byte_extent)
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
      'recorded trail %s (%d lines, transcript %s)',
      self.trail_id,
      self._recording.extent,
      self.record_id[:12],
    )
    return _Progress.ADVANCED

  def is_quiet(self) -> bool:
    """True when the transcript has not been written since the last read."""
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


class _PendingStart(NamedTuple):
  """a start the map holds and the store has not answered yet."""

  start: MappedStart
  spans: tuple[Span, ...]


class Recorder:
  """follows the transcript the session writes to and records each stretch of it
  into a trail of its own. `started_after` gates following to files modified
  after the session launched, so a reused workspace's older transcripts are not
  re-recorded."""

  def __init__(
    self,
    projects_dir: Path,
    client: TrailsStore,
    *,
    llm: dict,
    session: ManagedSession,
    started_after: float,
    recording_map: RecordingMap,
  ) -> None:
    self.projects_dir = projects_dir
    self.client = client
    self.llm = llm
    self.session = session
    self.started_after = started_after
    # read once: every trail of this recorder lifetime belongs to the same
    # summoned run, and the reader consumes the env var
    self.summoned_by = summoned_by_from_env()
    self._map = recording_map
    self._followed: Optional[Path] = None
    self._active: Optional[_StretchRecorder] = None
    self._start: Optional[_PendingStart] = None
    # the followed transcript as it stood when its last plan waited or persisted a start
    self._read: Optional[_Signature] = None
    self._refusal: Optional[Exception] = None
    # a stale pointer from a previous lifetime must not attribute this
    # session's summons to an ended trail
    trail_pointer.clear()

  # --- passes ---------------------------------------------------------------------

  def tick(self) -> bool:
    """one pass; True when a server write advanced the recording."""
    return self._guarded(self._pass)

  def finalize(self) -> bool:
    """the daemon's shutdown pass: one last pass, then end the trail."""
    return self._guarded(self._final_pass)

  def _guarded(self, step: Callable[[], bool]) -> bool:
    """run a pass; a refusal no later pass can change ends the recording."""
    if self._refusal is not None:
      raise self._refusal
    try:
      return step()
    except _REFUSALS as refusal:
      self._refusal = refusal
      raise

  def _final_pass(self) -> bool:
    changed = self._pass()
    if self._active is None:
      return changed
    self._end(self._active)
    return True

  def _pass(self) -> bool:
    candidate = self._pick_transcript()
    followed = self._followed
    changed = False
    if followed is not None and candidate is not None and candidate != followed:
      if self._is_quiet(followed):
        changed = self._leave()
      else:
        # a live followed transcript plus an unrelated newer jsonl: hold rather
        # than flip recording back and forth between two growing files
        log.warning(
          'transcript %s appeared while %s is still growing; holding',
          candidate.stem[:12],
          followed.stem[:12],
        )
    if self._followed is None:
      self._followed = candidate
    if self._followed is None:
      return changed
    return self._advance(self._followed) or changed

  def _advance(self, path: Path) -> bool:
    active = self._active
    if active is None:
      return self._begin(path)
    progress = active.tick()
    if progress is not _Progress.REWOUND:
      return progress is _Progress.ADVANCED
    log.warning(
      'transcript %s shrank below trail %s; ending it', active.record_id[:12], active.trail_id
    )
    self._end(active, forget=True)
    self._followed = None
    return True

  # --- following ------------------------------------------------------------------

  def _pick_transcript(self) -> Optional[Path]:
    if not self.projects_dir.is_dir():
      return None
    best: Optional[Path] = None
    best_mtime = 0.0
    for path in self.projects_dir.iterdir():
      if path.suffix != '.jsonl':
        continue
      try:
        mtime = path.stat().st_mtime
      except FileNotFoundError:
        continue
      if path != self._followed and mtime < self.started_after:
        continue
      if mtime > best_mtime:
        best = path
        best_mtime = mtime
    return best

  def _is_quiet(self, path: Path) -> bool:
    if _modified_at(path) < self.started_after:
      return True
    if self._active is not None:
      return self._active.is_quiet()
    signature = _signature(path)
    return signature is None or signature == self._read

  def _leave(self) -> bool:
    """stop following the transcript, ending the trail written to it."""
    ended = False
    if self._active is not None:
      self._end(self._active)
      ended = True
    self._followed = None
    self._start = None
    self._read = None
    return ended

  # --- trail lifecycle ------------------------------------------------------------

  def _begin(self, path: Path) -> bool:
    """start the trail the followed transcript's next stretch is recorded into,
    once it holds a line for it."""
    if self._start is None:
      signature = _signature(path)
      if signature is None or signature == self._read:
        return False
      plan = _Planner(self._map, self.client, self.projects_dir).plan(path.stem)
      if plan is None:
        self._read = signature
        return False
      start = MappedStart.prepare(
        self._map, path.stem, plan.spans, self._request(path.stem, plan.edge), closing=plan.closing
      )
      self._start = _PendingStart(start, plan.spans)
      self._read = signature
    recording = self._start.start.start(self.client)
    if recording.extent != 0:
      raise RuntimeError(
        f'started trail {recording.trail_id} already holds {recording.extent} rows'
      )
    signature = _signature(path)
    if signature is None:
      raise FileNotFoundError(f'transcript {path} disappeared before its trail started')
    lines, byte_extent = read_lines_after(path, 0)
    active = _StretchRecorder(
      path,
      recording,
      pending=_compose(lines, self._start.spans),
      position=_Position(signature, byte_extent),
    )
    edge = self._start.start.request.forked_from
    self._start = None
    self._read = None
    self._active = active
    if edge is None:
      log.info('trail %s opens at transcript %s (root)', active.trail_id, path.stem[:12])
    else:
      log.info(
        'trail %s opens at transcript %s (%s from %s @ %s)',
        active.trail_id,
        path.stem[:12],
        edge['kind'],
        edge['trail_id'],
        edge['step_id'],
      )
    trail_pointer.publish(active.trail_id)
    active.tick()
    return True

  def _request(self, record_id: str, edge: Optional[dict[str, Any]]) -> StartRequest:
    return StartRequest(
      harness='claude',
      version=configs.VERSION,
      interactive=True,
      surface='ride',
      native={
        'llm': self.llm,
        'segment': record_id,
        'ride_command': self.session.ride_command,
        'harness_version': 'unknown',
      },
      location=self.session.location,
      git=self.session.git,
      body={'records': []},
      bro=os.environ.get('RIDE_BRO'),
      hold=session_hold(),
      summoned_by=self.summoned_by,
      forked_from=edge,
    )

  def _end(self, active: _StretchRecorder, *, forget: bool = False) -> None:
    """end the trail and mark its entry ended, or remove the record's entries
    when `forget` is set."""
    active.close()
    entries = self._map.records[active.record_id]
    self._map.set_entries(
      active.record_id,
      []
      if forget
      else [
        replace(entry, ended=True) if entry.id == active.trail_id else entry for entry in entries
      ],
    )
    self._active = None
    trail_pointer.clear()


def _exception_summary(exception: BaseException) -> str:
  return f'{type(exception).__name__}: {exception}'


def _attempt(step: Callable[[], bool], *, interval: Optional[int], log_path: Path) -> None:
  """run one recorder pass and beat the health file with its outcome, quiet
  passes included — a beat that stops arriving is how a killed daemon is seen."""
  try:
    step()
  except Exception as e:
    log.exception('recording failed')
    health.write('error', _exception_summary(e), interval=interval, diagnostics=str(log_path))
    return
  health.write('ok', interval=interval, diagnostics=str(log_path))


def _watch(recorder: Recorder, interval: int, log_path: Path) -> None:
  """record on every transcript write and every `interval` seconds without one,
  until SIGTERM or the parent's exit."""
  stop = threading.Event()
  parent_pid = os.getppid()
  with watching(recorder.projects_dir) as changes:

    def _handle_signal(signum, frame):
      del signum, frame
      stop.set()
      changes.notify()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop.is_set():
      if os.getppid() != parent_pid:
        log.info('parent process exited, shutting down')
        break
      _attempt(recorder.tick, interval=interval, log_path=log_path)
      changes.wait(interval)

  _attempt(recorder.finalize, interval=None, log_path=log_path)


def record_session(
  projects_dir: Path,
  log_path: Path,
  interval: int = 3,
  llm: Optional[str] = None,
) -> int:
  session = managed_session()
  directory = session_dir()
  if session is None or directory is None:
    log.error('%s is unset: the trail recorder runs inside a managed session', SESSION_DIR_ENV)
    return 1
  try:
    llm_recipe = json.loads(llm) if llm is not None else {}
  except json.JSONDecodeError as e:
    log.error('invalid --llm json: %s', e)
    return 1

  try:
    recording_map = RecordingMap.load(directory)
  except RecordingMapError as e:
    log.error('%s', e)
    health.write('error', _exception_summary(e), interval=None, diagnostics=str(log_path))
    return 1

  try:
    client = default_store()
  except credentials.SecretNotFound:
    selected = credentials.default_store().selected_name('trails')
    log.error('config not found: trails (selected %s)', selected)
    health.write('error', 'config not found: trails', interval=None, diagnostics=str(log_path))
    return 1

  recorder = Recorder(
    projects_dir,
    client,
    llm=llm_recipe,
    session=session,
    started_after=transcript_clock_now(projects_dir),
    recording_map=recording_map,
  )
  log.info('recording %s (interval=%ds, workspace=%s)', projects_dir, interval, session.workspace)
  _watch(recorder, interval, log_path)
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='record a Claude Code session transcript to trails')
  parser.add_argument(
    '--interval',
    type=int,
    default=3,
    help='seconds between records when no transcript write comes sooner (default: 3)',
  )
  parser.add_argument(
    '--projects-dir',
    type=Path,
    required=True,
    help='the claude projects dir holding the session transcripts to record',
  )
  parser.add_argument(
    '--log-path',
    type=Path,
    required=True,
    help="where the launcher sends this daemon's output, named in its health beats",
  )
  parser.add_argument(
    '--llm',
    default=None,
    help='launch recipe json for native.llm, e.g. {"model": "...", "effort": "..."}',
  )
  return record_session(**parser.parse(argv))
