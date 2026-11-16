import contextlib
import copy
import json
from pathlib import Path

import pytest

from bro.monitor import health, trail_pointer
from bro.native import trail_recorder
from bro.native.run_log import STEP_KINDS
from bro.native.run_log_test_helper import open_log, session_environment
from bro.native.trail_recorder import Recorder
from bro.summon import SUMMONED_ENV, SUMMONER_ENV
from bro.trails.local import LocalStore
from bro.trails.model import tools_sha256
from bro.trails.record import spine
from bro.trails.record.session import managed_session
from bro.trails.store import AppendConflict, InvalidRequest, PermissionDenied, TrailNotFound


class Store(LocalStore):
  def __init__(self, root):
    super().__init__(root)
    self.appends = []
    self.blazes = []
    self.keepalives = []
    self.failure = None
    self.commit = 0
    self.on_append = lambda: None

  def blaze(self, request):
    self.blazes.append(request)
    return super().blaze(request)

  def append_records(self, trail_id, offset, records, *, tools=None):
    self.appends.append(copy.deepcopy((offset, records, tools)))
    self.on_append()
    if self.failure is not None:
      failure, self.failure = self.failure, None
      if self.commit:
        super().append_records(trail_id, offset, records[: self.commit], tools=tools)
      raise failure
    return super().append_records(trail_id, offset, records, tools=tools)

  def keepalive(self, trail_id):
    self.keepalives.append(trail_id)
    super().keepalive(trail_id)


@pytest.fixture
def setup(tmp_path, monkeypatch):
  for key, value in session_environment(tmp_path).items():
    monkeypatch.setenv(key, value)
  store = Store(tmp_path / 'store')
  path = tmp_path / 'run.jsonl'
  session = managed_session()
  assert session is not None
  recorder = Recorder(path, store, session=session, log_path=tmp_path / 'recorder.log')
  return path, store, recorder


def trail_id(recorder):
  assert recorder.recording is not None
  return recorder.recording.trail_id


def beat():
  path = health.health_path()
  assert path is not None
  return json.loads(path.read_text())


def call(writer, *, tools=None):
  return writer.step(
    'llm_call',
    {
      'request': {'tools': [] if tools is None else tools, 'model': 'echo'},
      'response': {'model': 'echo', 'text': 'reply'},
    },
    wire='echo',
    turn_index=0,
    call_index=1,
  )


def test_line_rows_tools_session_facts_pointer_and_beats(setup):
  path, store, recorder = setup
  writer = open_log(path)
  tools = [{'name': 'a'}]
  call(writer, tools=tools)
  assert recorder.tick()
  pointer = trail_pointer.path()
  assert pointer is not None
  identifier = trail_id(recorder)
  assert trail_pointer.read(pointer) == identifier
  assert beat()['status'] == 'ok'
  assert beat()['stale_after'] is not None
  assert beat()['diagnostics'] == str(recorder.log_path)
  call(writer, tools=tools)
  writer.end('ok')
  assert recorder.tick(final=True)
  rows = list(store.iter_steps(identifier))
  values = [
    line for line in path.read_text().splitlines() if json.loads(line)['kind'] in STEP_KINDS
  ]
  assert [row['body'] for row in rows] == values
  assert [row['step_id'] for row in rows] == list(range(len(values)))
  assert [row['ts'] for row in rows] == [json.loads(line)['at'] for line in values]
  assert all(
    row['run_log'] == json.loads(path.read_text().splitlines()[0])['format'] for row in rows
  )
  assert [tools_sha256(tools)] == [digest for _, _, blobs in store.appends for digest in blobs]
  assert store.get_tool(tools_sha256(tools)) == tools
  request = store.blazes[0]
  assert request.location == recorder.session.location
  assert request.native['ride_command'] == recorder.session.ride_command
  assert request.body == {'records': []}
  assert store.get_trail(identifier)['end']['reason'] == 'ok'
  assert trail_pointer.read(pointer) == identifier
  assert beat()['stale_after'] is None


@pytest.mark.parametrize(
  ('interactive', 'summoned', 'expected'),
  [(False, False, True), (True, True, True), (True, False, False)],
)
def test_trail_mark_selection(setup, monkeypatch, interactive, summoned, expected):
  path, store, recorder = setup
  marks = []

  class Lifecycle:
    def trail(self, identifier):
      marks.append(identifier)

    def close(self):
      pass

  monkeypatch.setattr(trail_recorder.RunLifecycle, 'from_env', lambda: Lifecycle())
  recorder.is_summoned = summoned
  open_log(path, interactive=interactive)
  recorder.tick(final=True)
  assert marks == ([trail_id(recorder)] if expected else [])


def test_unreachable_mark_is_not_retried_or_a_failed_pass(setup, monkeypatch):
  path, store, recorder = setup
  attempts = []

  def unavailable():
    attempts.append(True)
    raise OSError('broker down')

  monkeypatch.setattr(trail_recorder.RunLifecycle, 'from_env', unavailable)
  open_log(path)
  recorder.tick()
  recorder.tick()
  assert attempts == [True]
  assert beat()['status'] == 'ok'


def test_summoner_is_read_once(setup, monkeypatch):
  path, store, existing = setup
  edge = {'trail_id': 'parent', 'step_id': 2, 'index': 0}
  monkeypatch.setenv(SUMMONER_ENV, json.dumps(edge))
  monkeypatch.setenv(SUMMONED_ENV, '1')
  recorder = Recorder(path, store, session=existing.session, log_path=existing.log_path)
  monkeypatch.setenv(SUMMONER_ENV, 'not json')
  open_log(path)
  recorder.tick()
  assert store.blazes[0].summoned_by == edge


def test_absent_log_and_unended_log_keepalive(setup, monkeypatch):
  path, store, recorder = setup
  assert not recorder.tick()
  assert store.blazes == []
  open_log(path)
  recorder.tick()
  monkeypatch.setattr(spine, 'KEEPALIVE_INTERVAL_SECONDS', 0)
  recorder.tick(final=True)
  assert store.keepalives == [trail_id(recorder)]
  assert store.get_trail(trail_id(recorder))['end'] is None


def test_backlog_reads_only_after_batch_acknowledgment(setup, monkeypatch):
  path, store, recorder = setup
  monkeypatch.setattr(trail_recorder, 'IMPORT_CHUNK_ROWS', 4)
  writer = open_log(path)
  for index in range(20):
    writer.step('user_input', str(index))
  writer.end('ok')
  events = []
  read = recorder.reader.read

  def observed_read(**kwargs):
    events.append('read')
    return read(**kwargs)

  monkeypatch.setattr(recorder.reader, 'read', observed_read)
  store.on_append = lambda: events.append('append')
  recorder.tick(final=True)
  assert events == ['read', 'append'] * 6 + ['read']
  assert all(len(records) <= 4 for _, records, _ in store.appends)
  assert len(list(store.iter_steps(trail_id(recorder)))) == 21


def test_failed_batch_is_unchanged_and_no_tail_read_until_confirmed(setup, monkeypatch):
  path, store, recorder = setup
  monkeypatch.setattr(trail_recorder, 'IMPORT_CHUNK_ROWS', 3)
  writer = open_log(path)
  for index in range(10):
    writer.step('user_input', str(index))
  store.failure = OSError('store unavailable')
  recorder.tick()
  failed = copy.deepcopy(store.appends[-1])
  offset = recorder.reader.offset
  assert beat()['status'] == 'error'
  writer.step('user_input', 'tail')
  store.failure = OSError('still unavailable')
  recorder.tick()
  assert recorder.reader.offset == offset
  assert store.appends[-1] == failed
  writer.end('ok')
  recorder.tick(final=True)
  assert store.appends[2] == failed
  assert len(list(store.iter_steps(trail_id(recorder)))) == 12
  assert beat()['status'] == 'ok'


@pytest.mark.parametrize('commit', [2, 1000])
def test_lost_reply_and_partially_committed_batch_are_recovered(setup, commit):
  path, store, recorder = setup
  writer = open_log(path)
  tools = [{'name': 'a'}]
  call(writer, tools=tools)
  call(writer, tools=tools)
  writer.step('user_input', 'last')
  writer.end('ok')
  store.commit = commit
  store.failure = OSError('reply lost')
  recorder.tick()
  failed = copy.deepcopy(store.appends[0])
  recorder.tick(final=True)
  assert store.appends[1] == failed
  assert len(list(store.iter_steps(trail_id(recorder)))) == 4
  assert store.get_trail(trail_id(recorder))['end']['reason'] == 'ok'
  if commit == 2:
    assert [(offset, len(records)) for offset, records, _ in store.appends] == [
      (0, 4),
      (0, 4),
      (0, 2),
      (2, 2),
    ]
    assert store.appends[2][2] == {tools_sha256(tools): tools}
    assert store.appends[3][2] == {}


@pytest.mark.parametrize(
  'failure',
  [
    InvalidRequest('bad request'),
    PermissionDenied('denied'),
    TrailNotFound('gone'),
    AppendConflict(0, 0),
    AppendConflict(0, 100),
  ],
)
def test_permanent_store_refusals_stop_all_later_writes(setup, failure):
  path, store, recorder = setup
  writer = open_log(path)
  store.failure = failure
  recorder.tick()
  calls = len(store.appends)
  writer.step('user_input', 'later')
  recorder.tick(final=True)
  assert len(store.appends) == calls
  assert beat()['status'] == 'error'
  assert beat()['stale_after'] is None


def test_unverified_partial_prefix_ends_recording(setup, monkeypatch):
  path, store, recorder = setup
  writer = open_log(path)
  writer.step('user_input', 'next')
  store.commit = 1
  store.failure = OSError('partial commit')
  recorder.tick()
  append = store.append_records

  def not_duplicate(identifier, offset, records, **kwargs):
    result = append(identifier, offset, records, **kwargs)
    if len(records) == 1:
      result.pop('duplicate', None)
    return result

  monkeypatch.setattr(store, 'append_records', not_duplicate)
  recorder.tick()
  count = len(store.appends)
  recorder.tick()
  assert len(store.appends) == count
  assert 'not acknowledged as a duplicate' in beat()['error']


def test_nonzero_blaze_extent_is_permanent(setup, monkeypatch):
  path, store, recorder = setup
  blaze = store.blaze

  def nonzero(request):
    return {**blaze(request), 'extent': 1}

  monkeypatch.setattr(store, 'blaze', nonzero)
  open_log(path)
  recorder.tick()
  recorder.tick()
  assert len(store.blazes) == 1
  assert store.appends == []
  assert 'nonzero extent' in beat()['error']


def test_invalid_run_log_is_permanent(setup):
  path, store, recorder = setup
  writer = open_log(path)
  recorder.tick()
  with path.open('a') as stream:
    stream.write('{invalid}\n')
  recorder.tick()
  appends = len(store.appends)
  writer.step('user_input', 'later')
  recorder.tick(final=True)
  assert len(store.appends) == appends
  assert f'{path}:3:' in beat()['error']


def test_byte_bound_uses_encoded_line_rows(setup, monkeypatch):
  path, store, recorder = setup
  monkeypatch.setattr(trail_recorder, 'IMPORT_CHUNK_BYTES', 300)
  writer = open_log(path)
  for text in ['"' * 40, 'x' * 1000, 'last']:
    writer.step('user_input', text)
  writer.end('ok')
  recorder.tick()
  from bro.trails.model import canonical_json_bytes

  for _, records, _ in store.appends:
    assert len(records) == 1 or sum(len(canonical_json_bytes(record)) for record in records) <= 300
  assert len(list(store.iter_steps(trail_id(recorder)))) == 4


@pytest.mark.parametrize('first_entry', ['user_input', 'tools'])
def test_read_failure_does_not_consume_unreturned_entries(setup, monkeypatch, first_entry):
  path, store, recorder = setup
  writer = open_log(path)
  recorder.tick()
  position = recorder.reader.offset, recorder.reader.line_number
  if first_entry == 'user_input':
    writer.step('user_input', 'first input')
    writer.step('user_input', 'second input')
  else:
    call(writer, tools=[{'name': 'new tool'}])
  writer.end('ok')
  opened = Path.open

  class InterruptedRead:
    def __init__(self, stream):
      self.stream = stream
      self.lines = 0

    def seek(self, offset):
      return self.stream.seek(offset)

    def readline(self):
      self.lines += 1
      if self.lines == 2:
        raise OSError('interrupted file read')
      return self.stream.readline()

  @contextlib.contextmanager
  def failing_open(target, mode='r', *args, **kwargs):
    with opened(target, mode, *args, **kwargs) as stream:
      yield InterruptedRead(stream) if target == path and mode == 'rb' else stream

  with monkeypatch.context() as patch:
    patch.setattr(Path, 'open', failing_open)
    assert not recorder.tick()
  assert (recorder.reader.offset, recorder.reader.line_number) == position
  assert beat()['status'] == 'error'
  assert recorder.tick(final=True)
  values = [
    line for line in path.read_text().splitlines() if json.loads(line)['kind'] in STEP_KINDS
  ]
  rows = list(store.iter_steps(trail_id(recorder)))
  assert [row['body'] for row in rows] == values
  assert [row['step_id'] for row in rows] == list(range(len(values)))
  assert store.get_trail(trail_id(recorder))['end']['reason'] == 'ok'
  assert beat()['status'] == 'ok'
