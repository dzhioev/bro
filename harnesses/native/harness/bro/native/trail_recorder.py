"""Record a managed session's native run log into a trail."""

import contextlib
import json
import os
import signal
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

import watchfiles

from bro.base import credentials, log
from bro.base.args import Parser
from bro.monitor import SESSION_DIR_ENV, health, session_dir, trail_pointer
from bro.native.run_log import STEP_KINDS, Entry, Reader, RunLogError, private_directory, read
from bro.native_trails.format import STEP_LINE_FORMAT
from bro.run_lifecycle import RunLifecycle
from bro.summon import summoned, summoned_by_from_env
from bro.trails.model import StartRequest, canonical_json_bytes
from bro.trails.record.recording_map import RecordingMap, RecordingMapError, Span
from bro.trails.record.session import ManagedSession, managed_session
from bro.trails.record.settling import settle_record
from bro.trails.record.spine import MappedStart, Recording
from bro.trails.store import (
  IMPORT_CHUNK_BYTES,
  IMPORT_CHUNK_ROWS,
  AppendConflict,
  InvalidRequest,
  PermissionDenied,
  TrailCollision,
  TrailNotFound,
  TrailsStore,
  default_store,
)


class _Refusal(RuntimeError):
  pass


@dataclass
class _Batch:
  records: list[dict]
  tools: dict[str, Any]
  duplicate_only: bool = False

  def portion(self, start: int, stop: int, *, duplicate_only: bool = False) -> '_Batch':
    records = self.records[start:stop]
    digests: set[str] = set()
    for record in records:
      value = json.loads(record['body'])
      if value['kind'] == 'llm_call':
        digests.add(value['tools_sha256'])
    return _Batch(
      records, {key: value for key, value in self.tools.items() if key in digests}, duplicate_only
    )


class Recorder:
  def __init__(
    self,
    run_log: Path,
    store: TrailsStore,
    *,
    session: ManagedSession,
    log_path: Path,
    interval: float = 3,
    recording_map: Optional[RecordingMap] = None,
    historical: bool = False,
    ancestry: frozenset[Path] = frozenset(),
  ):
    self.reader = Reader(run_log)
    self.store = store
    self.session = session
    self.log_path = log_path
    self.interval = interval
    self.summoned_by = summoned_by_from_env()
    self.is_summoned = summoned()
    self.recording: Optional[Recording] = None
    self._map = recording_map
    self._historical = historical
    self._ancestry = ancestry | {run_log.resolve()}
    self._predecessor: Optional[Recorder] = None
    self._start: Optional[MappedStart] = None
    self._skip = 0
    self._entries: list[Entry] = []
    self._batches: deque[_Batch] = deque()
    self._tools: dict[str, Any] = {}
    self._end: Optional[dict] = None
    self._ended = False
    self._refusal: Optional[Exception] = None

  def tick(self, *, final: bool = False) -> bool:
    """Run one pass and publish its health, including permanent refusals."""
    try:
      if self._refusal is not None:
        raise self._refusal
      changed = self._advance()
    except Exception as exception:
      if isinstance(
        exception,
        (
          InvalidRequest,
          PermissionDenied,
          TrailCollision,
          TrailNotFound,
          RunLogError,
          RecordingMapError,
          _Refusal,
        ),
      ):
        self._refusal = exception
      log.exception('recording failed')
      summary = f'{type(exception).__name__}: {exception}'.splitlines()[0]
      health.write(
        'error', summary, interval=None if final else self.interval, diagnostics=str(self.log_path)
      )
      return False
    health.write('ok', interval=None if final else self.interval, diagnostics=str(self.log_path))
    return changed

  def _size(self, entry: Entry) -> int:
    if entry.value['kind'] in STEP_KINDS:
      return len(canonical_json_bytes({'run_log': STEP_LINE_FORMAT, 'body': entry.line}))
    return len(entry.line.encode())

  def _advance(self) -> bool:
    if self._map is None:
      directory = session_dir()
      if directory is None:
        raise RuntimeError('native recorder requires a session directory')
      self._map = RecordingMap.load(directory)
    changed = False
    while True:
      if len(self._batches) > 0:
        self._append()
        changed = True
        continue
      if len(self._entries) == 0:
        try:
          self._entries = self.reader.read(
            max_entries=IMPORT_CHUNK_ROWS, max_bytes=IMPORT_CHUNK_BYTES, size=self._size
          )
        except FileNotFoundError:
          if self.reader.offset != 0:
            raise _Refusal(f'run log disappeared: {self.reader.path}') from None
          break
      if len(self._entries) == 0:
        break
      for entry in self._entries:
        value = entry.value
        if value['kind'] == 'open':
          if self.recording is None:
            self._open(value)
            changed = True
        elif value['kind'] == 'tools':
          self._tools[value['sha256']] = value['tools']
        elif value['kind'] == 'end':
          self._end = value
      records = [
        {'run_log': STEP_LINE_FORMAT, 'body': entry.line}
        for entry in self._entries
        if entry.step_number is not None
      ]
      digests = {
        entry.value['tools_sha256'] for entry in self._entries if entry.value['kind'] == 'llm_call'
      }
      skipped = min(self._skip, len(records))
      records = records[skipped:]
      self._skip -= skipped
      if len(records) > 0:
        self._batches.append(
          _Batch(
            records, {digest: self._tools[digest] for digest in digests if digest in self._tools}
          )
        )
      self._entries = []
    if self.recording is not None:
      if self._end is not None and not self._ended:
        self.recording.end(self._end['reason'], self._end.get('detail'))
        self._mark_ended()
        self._ended = True
        changed = True
      if not changed and not self._ended:
        self.recording.keepalive_if_idle()
    return changed

  def _mark_ended(self) -> None:
    assert self._map is not None
    identifier = self.reader.path.stem
    entries = self._map.records[identifier]
    self._map.set_entries(identifier, [replace(entry, ended=True) for entry in entries])

  def _open(self, value: dict) -> None:
    assert self._map is not None
    identifier = self.reader.path.stem
    if self._start is None:
      entries = self._map.records.get(identifier, [])
      if entries:
        if len(entries) != 1 or entries[0].spans != (Span(0),):
          raise _Refusal(f'native run log {identifier} requires one trail spanning from step 0')
        snapshot = read(self.reader.path)
        rows = [
          {'run_log': STEP_LINE_FORMAT, 'body': entry.line}
          for entry in snapshot.entries
          if entry.step_number is not None
        ]
        settled = settle_record(self._map, identifier, self.store, rows)
        if settled is not None:
          self.recording = Recording(self.store, settled.entry.id, settled.extent)
          self._skip = settled.extent
          self._ended = settled.entry.ended
          return
      edge = None
      pointer = value.get('forked_from')
      if pointer is not None:
        parent = self.reader.path.with_name(pointer['run'] + '.jsonl')
        if parent.resolve() in self._ancestry:
          raise _Refusal(f'run log chain contains a cycle: {parent}')
        try:
          previous = read(parent)
        except FileNotFoundError as exception:
          raise _Refusal(
            f'{self.reader.path}: forked_from names missing run log {parent}'
          ) from exception
        if pointer['step'] >= len(previous.steps):
          raise _Refusal(f'{self.reader.path}: missing step {pointer["step"]} in {parent}')
        if self._predecessor is None:
          self._predecessor = Recorder(
            parent,
            self.store,
            session=self.session,
            log_path=self.log_path,
            interval=self.interval,
            recording_map=self._map,
            historical=True,
            ancestry=self._ancestry,
          )
        self._predecessor._advance()
        recording = self._predecessor.recording
        assert recording is not None
        edge = {'trail_id': recording.trail_id, 'step_id': pointer['step'], 'kind': 'resume'}
      request = StartRequest(
        harness='bro',
        bro=value['bro'],
        version=value['version'],
        interactive=value['interactive'],
        surface=value['surface'],
        hold=value['hold'],
        forked_from=edge,
        summoned_by=None if self._historical else self.summoned_by,
        native={
          'llm': value['recipe'],
          **({} if self._historical else {'ride_command': self.session.ride_command}),
        },
        location=self.session.location,
        git=None if self._historical else self.session.git,
        body={'records': []},
      )
      self._start = MappedStart.prepare(self._map, identifier, (Span(0),), request)
    recording = self._start.start(self.store)
    if type(recording.extent) is not int or recording.extent != 0:
      raise _Refusal(f'fresh trail {recording.trail_id} has nonzero extent {recording.extent}')
    self.recording = recording
    if self._historical:
      return
    trail_pointer.publish(recording.trail_id)
    if not value['interactive'] or self.is_summoned:
      try:
        lifecycle = RunLifecycle.from_env()
        if lifecycle is not None:
          with contextlib.closing(lifecycle):
            lifecycle.trail(recording.trail_id)
      except Exception:
        log.exception('cannot send trail mark')

  def _append(self) -> None:
    assert self.recording is not None
    batch = self._batches[0]
    offset = self.recording.extent
    try:
      if batch.duplicate_only:
        response = self.store.append_records(
          self.recording.trail_id, offset, batch.records, tools=batch.tools
        )
        extent = offset + len(batch.records)
        if response.get('duplicate') is not True or response.get('extent') != extent:
          raise _Refusal('partially kept batch prefix was not acknowledged as a duplicate')
        self.recording = Recording(self.store, self.recording.trail_id, extent)
      else:
        self.recording.append(batch.records, tools=batch.tools)
    except AppendConflict as exception:
      count = exception.actual - offset
      if batch.duplicate_only or not 0 < count < len(batch.records):
        raise _Refusal(str(exception)) from exception
      self._batches.popleft()
      self._batches.appendleft(batch.portion(count, len(batch.records)))
      self._batches.appendleft(batch.portion(0, count, duplicate_only=True))
      return
    self._batches.popleft()
    for digest in batch.tools:
      self._tools.pop(digest, None)
      for pending in self._batches:
        pending.tools.pop(digest, None)


@contextlib.contextmanager
def _stop_handlers() -> Iterator[threading.Event]:
  stop = threading.Event()

  def handle(signum, frame):
    del signum, frame
    stop.set()

  previous = {number: signal.signal(number, handle) for number in (signal.SIGTERM, signal.SIGINT)}
  try:
    yield stop
  finally:
    for number, handler in previous.items():
      signal.signal(number, handler)


def record_session(run_log: Path, log_path: Path, ready_fd: int, interval: float = 3) -> int:
  if interval <= 0:
    raise ValueError('interval must be positive')
  parent = os.getppid()
  with _stop_handlers() as stop:
    with os.fdopen(ready_fd, 'wb') as ready:
      ready.write(b'1')
    try:
      session = managed_session()
      if session is None:
        raise RuntimeError(
          f'{SESSION_DIR_ENV} is unset: the trail recorder requires a managed session'
        )
      store = default_store()
    except Exception as exception:
      summary = (
        'config not found: trails'
        if isinstance(exception, credentials.SecretNotFound)
        else f'{type(exception).__name__}: {exception}'
      )
      log.exception('cannot open recording store')
      health.write('error', summary.splitlines()[0], interval=None, diagnostics=str(log_path))
      return 1
    with contextlib.closing(store):
      recorder = Recorder(run_log, store, session=session, log_path=log_path, interval=interval)
      private_directory(run_log.parent)
      with contextlib.closing(
        watchfiles.watch(
          run_log.parent,
          stop_event=stop,
          yield_on_timeout=True,
          rust_timeout=max(1, int(interval * 1000)),
          debounce=50,
          step=10,
          watch_filter=None,
        )
      ) as changes:
        if not stop.is_set() and os.getppid() == parent:
          recorder.tick()
        for _ in changes:
          if stop.is_set() or os.getppid() != parent:
            break
          recorder.tick()
      recorder.tick(final=True)
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description="record a managed session's native run log to trails")
  parser.add_argument('--run-log', type=Path, required=True, help='the run log to follow')
  parser.add_argument('--log-path', type=Path, required=True, help="the daemon's diagnostic log")
  parser.add_argument(
    '--ready-fd', type=int, required=True, help='pipe descriptor for the readiness byte'
  )
  parser.add_argument('--interval', type=float, default=3, help='seconds between quiet passes')
  return record_session(**parser.parse(argv))
