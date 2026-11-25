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
from bro.native_trails.format import STEP_LINE_FORMAT
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
    self.starts = []
    self.keepalives = []
    self.failure = None
    self.commit = 0
    self.on_append = lambda: None

  def start_trail(self, request):
    self.starts.append(request)
    return super().start_trail(request)

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
  assert all(row['run_log'] == STEP_LINE_FORMAT for row in rows)
  assert [tools_sha256(tools)] == [digest for _, _, blobs in store.appends for digest in blobs]
  assert store.get_tool(tools_sha256(tools)) == tools
  request = store.starts[0]
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
  assert store.starts[0].summoned_by == edge


def test_absent_log_and_unended_log_keepalive(setup, monkeypatch):
  path, store, recorder = setup
  assert not recorder.tick()
  assert store.starts == []
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


def test_nonzero_start_extent_is_permanent(setup, monkeypatch):
  path, store, recorder = setup
  start_trail = store.start_trail

  def nonzero(request):
    return {**start_trail(request), 'extent': 1}

  monkeypatch.setattr(store, 'start_trail', nonzero)
  open_log(path)
  recorder.tick()
  recorder.tick()
  assert len(store.starts) == 1
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


@pytest.mark.parametrize('progress', ['complete', 'behind', 'unknown', 'unended'])
def test_predecessor_is_completed_before_a_resume_trail_starts(setup, progress):
  from bro.llm.tracker import RunFork
  from bro.native.run_log import RunLog
  from bro.trails.record.recording_map import RecordingMap

  path, store, recorder = setup
  writer = open_log(path)
  if progress in ('complete', 'behind', 'unended'):
    assert recorder.tick()
    old_id = trail_id(recorder)
  else:
    old_id = None
  call(writer, tools=[{'name': 'older_tool'}])
  writer.step('user_input', 'last input')
  if progress != 'unended':
    writer.end('ok')
  if progress == 'complete':
    assert recorder.tick()
  child_path = path.with_name('continued.jsonl')
  child = RunLog(child_path)
  child.start(
    'dev',
    {'type': 'echo', 'model': 'echo'},
    'continued prompt',
    RunFork(path.stem, 2),
    True,
    'test',
  )
  child.step('user_input', 'continue')
  child.end('ok')
  continuation = Recorder(child_path, store, session=recorder.session, log_path=recorder.log_path)
  assert continuation.tick(final=True)
  assert continuation._map is not None
  mapping = RecordingMap.load(continuation._map.path.parent)
  predecessor = mapping.records[path.stem][0]
  if old_id is not None:
    assert predecessor.id == old_id
  old_rows = list(store.iter_steps(predecessor.id))
  assert len(old_rows) == 3
  assert [json.loads(row['body'])['kind'] for row in old_rows] == [
    'system_prompt',
    'llm_call',
    'user_input',
  ]
  edge = store.get_trail(trail_id(continuation))['forked_from']
  assert edge == {'trail_id': predecessor.id, 'step_id': 2, 'kind': 'resume'}
  assert predecessor.ended == (progress != 'unended')
  assert mapping.records[child_path.stem][0].ended
  if progress == 'unknown':
    header = store.get_trail(predecessor.id)
    assert header.get('git') is None and header.get('summoned_by') is None
    assert 'ride_command' not in header['native']


def test_two_unrecorded_ancestors_are_recorded_oldest_first(setup):
  from bro.llm.tracker import RunFork
  from bro.native.run_log import RunLog

  path, store, recorder = setup
  root = open_log(path)
  root.step('user_input', 'root')
  root.end('ok')
  middle_path = path.with_name('middle.jsonl')
  middle = RunLog(middle_path)
  middle.start(
    'dev', {'type': 'echo', 'model': 'echo'}, 'middle', RunFork(path.stem, 1), True, 'test'
  )
  middle.step('user_input', 'middle')
  middle.end('ok')
  leaf_path = path.with_name('leaf.jsonl')
  leaf = RunLog(leaf_path)
  leaf.start('dev', {'type': 'echo', 'model': 'echo'}, 'leaf', RunFork('middle', 1), True, 'test')
  continuation = Recorder(leaf_path, store, session=recorder.session, log_path=recorder.log_path)
  assert continuation.tick()
  assert len(store.starts) == 3
  first, second, third = store.starts
  assert first.forked_from is None
  assert second.forked_from == {'trail_id': first.id, 'step_id': 1, 'kind': 'resume'}
  assert third.forked_from == {'trail_id': second.id, 'step_id': 1, 'kind': 'resume'}


def test_lost_start_reply_retries_the_same_mapped_identity(setup, monkeypatch):
  path, store, recorder = setup
  open_log(path).end('ok')
  start = store.start_trail
  requests = []

  def lost_reply(request):
    requests.append(request)
    response = start(request)
    if len(requests) == 1:
      raise OSError('reply lost')
    return response

  monkeypatch.setattr(store, 'start_trail', lost_reply)
  assert not recorder.tick()
  assert recorder.tick()
  assert requests[0] == requests[1]
  assert len(list(store.iter_trails())) == 1


@pytest.mark.parametrize('predecessor', [False, True])
def test_a_colliding_named_start_permanently_refuses_recording(setup, monkeypatch, predecessor):
  from bro.llm.tracker import RunFork
  from bro.native.run_log import RunLog
  from bro.trails.model import StartRequest
  from bro.trails.store import TrailCollision

  path, store, recorder = setup
  occupied = store.start_trail(
    StartRequest(
      harness='bro',
      version='test',
      bro='occupied',
      interactive=False,
      surface='test',
      native={'llm': {'type': 'echo', 'model': 'echo'}},
      body={'records': []},
    )
  )['id']
  monkeypatch.setattr(spine, 'lulid', lambda: occupied)
  writer = open_log(path)
  writer.step('user_input', 'earlier')
  if predecessor:
    child_path = path.with_name('child.jsonl')
    child = RunLog(child_path)
    child.start(
      'dev', {'type': 'echo', 'model': 'echo'}, 'prompt', RunFork(path.stem, 1), True, 'test'
    )
    recorder = Recorder(child_path, store, session=recorder.session, log_path=recorder.log_path)
  assert not recorder.tick()
  assert isinstance(recorder._refusal, TrailCollision)
  calls = len(store.starts), len(store.appends), len(store.keepalives)
  writer.step('user_input', 'later')
  assert not recorder.tick(final=True)
  assert (len(store.starts), len(store.appends), len(store.keepalives)) == calls
  assert beat()['status'] == 'error'
  assert beat()['stale_after'] is None
