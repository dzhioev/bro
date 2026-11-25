import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from bro.claude.trail_recorder import (
  Recorder,
  _attempt,
  _compose,
  _Position,
  _Progress,
  _signature,
  _StretchRecorder,
)
from bro.claude.transcripts import read_lines_after
from bro.monitor import health, session_dir, trail_pointer
from bro.trails.local import LocalStore
from bro.trails.model import StartRequest
from bro.trails.record.recording_map import FILENAME, RecordingMap, Span
from bro.trails.record.session import managed_session
from bro.trails.record.spine import Recording
from bro.trails.store import TrailCollision, TransientUnavailable


class _Store(LocalStore):
  """a real local store that also records the starts and keepalives it was
  sent, fails the number of appends `refusals` is set to, and loses the reply to
  the number of starts `lost_replies` is set to."""

  def __init__(self, root: Path):
    super().__init__(root)
    self.starts: list[StartRequest] = []
    self.keepalives: list[str] = []
    self.refusals = 0
    self.lost_replies = 0

  def start_trail(self, request: StartRequest) -> dict:
    self.starts.append(request)
    response = super().start_trail(request)
    if self.lost_replies > 0:
      self.lost_replies -= 1
      raise TransientUnavailable('reply lost')
    return response

  def keepalive(self, trail_id: str) -> None:
    self.keepalives.append(trail_id)
    super().keepalive(trail_id)

  def append_records(self, trail_id: str, offset: int, records: list[Any], **kwargs: Any) -> dict:
    if self.refusals > 0:
      self.refusals -= 1
      raise RuntimeError('store is down')
    return super().append_records(trail_id, offset, records, **kwargs)


def _record(**fields: Any) -> str:
  return json.dumps({'version': '2.1.216', 'timestamp': '2026-01-01T00:00:00.000Z', **fields})


def _user(text: str, uuid: str, **fields: Any) -> str:
  return _record(type='user', uuid=uuid, message={'content': text}, **fields)


def _assistant(text: str, uuid: str, *, message_id: str = 'msg-1', usage: Optional[dict] = None):
  return _record(
    type='assistant',
    uuid=uuid,
    message={
      'id': message_id,
      'model': 'claude-fable-5',
      'usage': usage if usage is not None else {'input_tokens': 1, 'output_tokens': 2},
      'content': [{'type': 'text', 'text': text}],
    },
  )


def _raise_call(reason: str) -> str:
  return _record(
    type='assistant',
    uuid='a1',
    message={
      'id': 'm1',
      'model': 'claude-fable-5',
      'usage': {'input_tokens': 1, 'output_tokens': 1},
      'content': [{'type': 'tool_use', 'name': 'mcp__bro__raise', 'input': {'reason': reason}}],
    },
  )


def _interrupt_notice() -> str:
  return _record(
    type='user',
    uuid='i1',
    message={'content': [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]},
    interruptedMessageId='m1',
  )


_EPHEMERA = json.dumps({'type': 'mode', 'mode': 'normal'})


def _branched(lines: list[str], origin: str, session_id: str) -> list[str]:
  """`lines` as `/branch` copies them into `session_id`: each record keeps its
  uuid and names where it was copied from."""
  copied = []
  for line in lines:
    value = json.loads(line)
    if 'uuid' in value:
      value = {
        **value,
        'sessionId': session_id,
        'forkedFrom': {'sessionId': origin, 'messageUuid': value['uuid']},
      }
    copied.append(json.dumps(value))
  return copied


def _forked(lines: list[str], session_id: str) -> list[str]:
  """`lines` as `--fork-session` copies them into `session_id`: the records keep
  their uuid, the lines without one are dropped, and nothing names the origin."""
  return [
    json.dumps({**json.loads(line), 'sessionId': session_id})
    for line in lines
    if 'uuid' in json.loads(line)
  ]


def _pointer() -> Path:
  path = trail_pointer.path()
  assert path is not None
  return path


@pytest.fixture
def environment(tmp_path: Path, monkeypatch):
  config = tmp_path / 'config'
  projects = config / 'projects' / '-tree'
  projects.mkdir(parents=True)
  monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
  monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
  monkeypatch.setenv('RIDE_WORKSPACE', 'ws')
  monkeypatch.setenv('RIDE_HOST', 'laptop')
  monkeypatch.setenv('RIDE_HOST_TREE', '/home/u/ws/tree')
  monkeypatch.setenv('RIDE_ISOLATION', 'unboxed')
  monkeypatch.setenv('RIDE_COMMAND', 'ride along ws')
  monkeypatch.setenv('RIDE_REPO', '/source/bro')
  monkeypatch.setenv('RIDE_REPO_URL', 'https://example.test/dzhioev/bro.git')
  monkeypatch.setenv('RIDE_BRANCH', 'workspace-ws')
  monkeypatch.setenv('RIDE_BASE_SHA', 'abc')
  monkeypatch.setenv('RIDE_BRO', 'dev')
  monkeypatch.setenv('BRO_HOLD', 'attended')
  monkeypatch.delenv('RIDE_SUMMONER', raising=False)
  return projects


@pytest.fixture
def store(tmp_path: Path) -> _Store:
  return _Store(tmp_path / 'trails')


def _session_dir() -> Path:
  directory = session_dir()
  assert directory is not None
  return directory


def _recorder(projects: Path, store: LocalStore, *, started_after: float = 0.0) -> Recorder:
  """a recorder lifetime over the session's recording map as it stands."""
  session = managed_session()
  assert session is not None
  return Recorder(
    projects,
    store,
    llm={'model': 'claude-fable-5'},
    session=session,
    started_after=started_after,
    recording_map=RecordingMap.load(_session_dir()),
  )


def _lifetime(projects: Path, store: LocalStore) -> None:
  """one recorder lifetime that records the session's transcript and ends."""
  recorder = _recorder(projects, store)
  recorder.tick()
  recorder.finalize()


def _write(projects: Path, record_id: str, lines: list[str]) -> Path:
  path = projects / f'{record_id}.jsonl'
  path.write_text('\n'.join(lines) + '\n')
  return path


def _entries(record_id: str) -> list[dict]:
  """the record's entries in the session's recording map, as stored."""
  stored = json.loads((_session_dir() / FILENAME).read_text())['records']
  return stored[record_id]['trails'] if record_id in stored else []


def _rows(store: LocalStore, trail_id: str) -> list[str]:
  return [step['body'] for step in store.iter_steps(trail_id)]


def _worker(store: LocalStore, path: Path) -> _StretchRecorder:
  """a stretch recorder over the whole of `path`, on a trail started as its own root."""
  request = StartRequest(
    harness='claude',
    version='test',
    interactive=True,
    surface='ride',
    native={
      'llm': {},
      'segment': path.stem,
      'ride_command': 'ride along',
      'harness_version': 'test',
    },
    body={'records': []},
  )
  lines, byte_extent = read_lines_after(path, 0)
  signature = _signature(path)
  assert signature is not None
  return _StretchRecorder(
    path,
    Recording.create(store, request),
    pending=_compose(lines, [Span(0)]),
    position=_Position(signature, byte_extent),
  )


class TestFollowing:
  def test_a_fresh_transcript_becomes_a_root_trail_under_the_mapped_id(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-1')
    assert entry['spans'] == [[0, None]]
    assert entry['forked_from'] is None
    [request] = store.starts
    assert request.id == entry['id']
    assert request.lineage is None
    header = store.get_trail(entry['id'])
    assert header['harness'] == 'claude'
    assert header['surface'] == 'ride'
    assert header['interactive'] is True
    assert header['bro'] == 'dev'
    assert header['hold'] == 'attended'
    assert 'forked_from' not in header
    assert header['native']['segment'] == 'seg-1'
    assert header['native']['ride_command'] == 'ride along ws'
    assert header['native']['llm'] == {'model': 'claude-fable-5'}
    session = managed_session()
    assert session is not None
    assert header['location'] == session.location
    assert header['git'] == session.git
    assert _rows(store, entry['id']) == lines

  def test_a_detached_session_records_no_git_state(self, environment, store, monkeypatch):
    monkeypatch.delenv('RIDE_REPO')
    monkeypatch.delenv('RIDE_REPO_URL')
    monkeypatch.delenv('RIDE_BRANCH')
    monkeypatch.delenv('RIDE_BASE_SHA')
    _write(environment, 'seg-1', [_user('hello', 'u1'), _assistant('hi', 'a1')])

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-1')
    assert 'git' not in store.get_trail(entry['id'])

  def test_transcripts_older_than_the_launch_are_not_followed(self, environment, store):
    path = _write(environment, 'seg-old', [_user('old', 'u1')])
    os.utime(path, (1, 1))

    assert _recorder(environment, store, started_after=100.0).tick() is False
    assert store.starts == []

  def test_a_transcript_of_bare_ephemera_starts_no_trail(self, environment, store):
    _write(environment, 'seg-1', [_EPHEMERA])
    recorder = _recorder(environment, store)

    assert recorder.tick() is False
    assert recorder.finalize() is False
    assert store.starts == []

  def test_the_pointer_tracks_the_started_trail(self, environment, store):
    _write(environment, 'seg-1', [_user('hello', 'u1')])

    _recorder(environment, store).tick()

    [entry] = _entries('seg-1')
    assert trail_pointer.read(_pointer()) == entry['id']

  def test_summoner_attribution_stamps_the_started_trail(self, environment, store, monkeypatch):
    monkeypatch.setenv('RIDE_SUMMONER', '{"trail_id": "t-parent", "step_id": 3}')
    _write(environment, 'seg-1', [_user('hello', 'u1'), _assistant('hi', 'a1')])

    recorder = _recorder(environment, store)
    # consumed on read: claude's own subprocesses must not inherit it
    assert os.environ.get('RIDE_SUMMONER') is None
    recorder.tick()

    [entry] = _entries('seg-1')
    assert store.get_trail(entry['id'])['summoned_by'] == {'trail_id': 't-parent', 'step_id': 3}

  def test_a_stale_pointer_is_cleared_at_start(self, environment, store):
    trail_pointer.publish('STALE')

    _recorder(environment, store)

    assert trail_pointer.read(_pointer()) is None


class TestAppends:
  """the stretch recorder alone: one file, one trail, no transcript selection."""

  def test_the_first_tick_records_the_pending_lines(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    worker = _worker(store, _write(environment, 'seg-1', lines))

    assert worker.tick() is _Progress.ADVANCED
    assert worker.tick() is _Progress.QUIET

    assert _rows(store, worker.trail_id) == lines

  def test_growth_appends_only_new_lines(self, environment, store):
    lines = [_user('hello', 'u1')]
    path = _write(environment, 'seg-1', lines)
    worker = _worker(store, path)
    worker.tick()
    lines.append(_assistant('hi', 'a1'))
    path.write_text('\n'.join(lines) + '\n')

    assert worker.tick() is _Progress.ADVANCED

    assert _rows(store, worker.trail_id) == lines

  def test_incomplete_line_waits_for_its_newline(self, environment, store):
    first = _user('hello', 'u1')
    path = _write(environment, 'seg-1', [first])
    worker = _worker(store, path)
    worker.tick()
    second = _assistant('hi', 'a1')
    path.write_text(first + '\n' + second)

    assert worker.tick() is _Progress.QUIET
    assert _rows(store, worker.trail_id) == [first]

    path.write_text(first + '\n' + second + '\n')
    assert worker.tick() is _Progress.ADVANCED
    assert _rows(store, worker.trail_id) == [first, second]

  def test_a_refused_append_is_retried_whole(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    worker = _worker(store, _write(environment, 'seg-1', lines))
    store.refusals = 1

    with pytest.raises(RuntimeError):
      worker.tick()

    assert worker.tick() is _Progress.ADVANCED
    assert _rows(store, worker.trail_id) == lines

  def test_a_transcript_shrinking_below_the_trail_rewinds(self, environment, store):
    path = _write(environment, 'seg-1', [_user('hello', 'u1'), _assistant('hi', 'a1')])
    worker = _worker(store, path)
    worker.tick()
    _write(environment, 'seg-1', [_user('different', 'x1')])

    assert worker.tick() is _Progress.REWOUND

  def test_quiet_tick_keepalives_after_the_idle_interval(self, environment, store):
    worker = _worker(store, _write(environment, 'seg-1', [_user('hello', 'u1')]))
    worker.tick()

    assert worker.tick() is _Progress.QUIET
    assert store.keepalives == []  # inside the idle window: no traffic

    worker._recording._last_write_monotonic = time.monotonic() - 120.0
    assert worker.tick() is _Progress.QUIET
    assert store.keepalives == [worker.trail_id]


class TestResume:
  """a transcript the map knows: each lifetime that writes to it records a trail
  of its own, continuing the transcript's last trail."""

  def test_a_resumed_transcript_records_a_new_trail_with_a_resume_edge(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)
    _lifetime(environment, store)
    [first] = _entries('seg-1')
    assert first['ended'] is True
    appended = [_user('again', 'u2'), _assistant('sure', 'a2')]
    _write(environment, 'seg-1', lines + appended)

    assert _recorder(environment, store).tick() is True

    previous, current = _entries('seg-1')
    assert previous['spans'] == [[0, 2]]
    assert current['spans'] == [[2, None]]
    edge = {'trail_id': first['id'], 'step_id': 1, 'kind': 'resume'}
    assert current['forked_from'] == edge
    assert store.get_trail(current['id'])['forked_from'] == edge
    assert _rows(store, current['id']) == appended
    assert _rows(store, first['id']) == lines
    assert store.get_trail(first['id'])['end']['reason'] == 'ok'
    assert trail_pointer.read(_pointer()) == current['id']

  def test_the_lines_a_stopped_recorder_missed_open_the_next_trail(self, environment, store):
    recorded = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    path = _write(environment, 'seg-1', recorded)
    _recorder(environment, store).tick()
    [first] = _entries('seg-1')
    # the recorder is gone before it reads these, and before it ends its trail
    missed = [_user('unrecorded', 'u2'), _assistant('also', 'a2')]
    _write(environment, 'seg-1', recorded + missed)
    resumed = [_user('resumed', 'u3')]
    _write(environment, 'seg-1', recorded + missed + resumed)

    assert _recorder(environment, store).tick() is True

    previous, current = _entries('seg-1')
    assert previous == {**first, 'spans': [[0, 2]]}
    assert current['forked_from'] == {'trail_id': first['id'], 'step_id': 1, 'kind': 'resume'}
    assert _rows(store, current['id']) == missed + resumed
    assert path.read_text().splitlines() == _rows(store, first['id']) + _rows(store, current['id'])

  def test_a_resumed_transcript_waits_for_a_line_of_its_own(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)
    _lifetime(environment, store)
    recorder = _recorder(environment, store)

    assert recorder.tick() is False
    assert recorder.tick() is False
    assert recorder.finalize() is False
    assert len(store.starts) == 1
    assert len(_entries('seg-1')) == 1

  def test_a_rewritten_transcript_loses_its_entries_and_records_a_root(self, environment, store):
    _write(environment, 'seg-1', [_user('hello', 'u1'), _assistant('hi', 'a1')])
    _lifetime(environment, store)
    [first] = _entries('seg-1')
    rewritten = [_user('different', 'x1'), _user('content', 'x2'), _user('again', 'x3')]
    _write(environment, 'seg-1', rewritten)

    assert _recorder(environment, store).tick() is True

    [root] = _entries('seg-1')
    assert root['id'] != first['id']
    assert root['spans'] == [[0, None]]
    assert root['forked_from'] is None
    assert _rows(store, root['id']) == rewritten

  def test_a_transcript_unknown_to_the_map_is_recorded_whole_into_a_root(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1'), _user('again', 'u2')]
    path = _write(environment, 'seg-1', lines[:2])
    # a trail an earlier recorder kept for the transcript, with no map entry
    earlier = _worker(store, path)
    earlier.close()
    _write(environment, 'seg-1', lines)

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-1')
    assert entry['id'] != earlier.trail_id
    assert entry['forked_from'] is None
    assert _rows(store, entry['id']) == lines
    assert _rows(store, earlier.trail_id) == lines[:2]


class TestCopies:
  """a transcript the map does not know, which copies one it knows."""

  def _recorded(self, projects: Path, store: LocalStore, record_id: str, lines: list[str]) -> str:
    _write(projects, record_id, lines)
    _lifetime(projects, store)
    return _entries(record_id)[-1]['id']

  def test_a_branch_records_its_own_lines_with_a_branch_edge(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    origin = self._recorded(environment, store, 'seg-1', lines)
    own = [_user('branched', 'u2')]
    _write(environment, 'seg-2', [_EPHEMERA, *_branched(lines, 'seg-1', 'seg-2'), *own])

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-2')
    assert entry['spans'] == [[0, 1], [3, None]]
    edge = {'trail_id': origin, 'step_id': 1, 'kind': 'branch'}
    assert entry['forked_from'] == edge
    assert store.get_trail(entry['id'])['forked_from'] == edge
    assert _rows(store, entry['id']) == [_EPHEMERA, *own]

  def test_a_forked_session_is_found_by_its_record_uuids(self, environment, store):
    lines = [_EPHEMERA, _user('hello', 'u1'), _assistant('hi', 'a1')]
    origin = self._recorded(environment, store, 'seg-1', lines)
    head = [json.dumps({'type': 'permission-mode', 'permissionMode': 'default'})]
    own = [_user('forked', 'u2')]
    _write(environment, 'seg-2', [*head, *_forked(lines, 'seg-2'), *own])

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-2')
    assert entry['spans'] == [[0, 1], [3, None]]
    assert entry['forked_from'] == {'trail_id': origin, 'step_id': 2, 'kind': 'branch'}
    assert _rows(store, entry['id']) == [*head, *own]

  def test_a_copy_is_not_started_while_it_is_being_written(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    origin = self._recorded(environment, store, 'seg-1', lines)
    copied = _branched(lines, 'seg-1', 'seg-2')
    recorder = _recorder(environment, store)

    # the copy arrives in stages: head ephemera, the copied records one by one,
    # then a record of its own
    _write(environment, 'seg-2', [_EPHEMERA])
    assert recorder.tick() is False
    _write(environment, 'seg-2', [_EPHEMERA, copied[0]])
    assert recorder.tick() is False
    _write(environment, 'seg-2', [_EPHEMERA, *copied])
    assert recorder.tick() is False
    assert _entries('seg-2') == []

    own = [_user('branched', 'u2')]
    _write(environment, 'seg-2', [_EPHEMERA, *copied, *own])
    assert recorder.tick() is True

    [entry] = _entries('seg-2')
    assert entry['forked_from'] == {'trail_id': origin, 'step_id': 1, 'kind': 'branch'}
    assert _rows(store, entry['id']) == [_EPHEMERA, *own]

  def test_copied_lines_the_origin_never_recorded_are_recorded_by_the_copy(
    self, environment, store
  ):
    recorded = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', recorded)
    _recorder(environment, store).tick()
    [origin] = _entries('seg-1')
    missed = [_user('unrecorded', 'u2'), _assistant('also', 'a2')]
    _write(environment, 'seg-1', recorded + missed)
    copied = _branched(recorded + missed, 'seg-1', 'seg-2')
    own = [_user('branched', 'u3')]
    _write(environment, 'seg-2', [*copied, *own])

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-2')
    assert entry['spans'] == [[2, None]]
    assert entry['forked_from'] == {'trail_id': origin['id'], 'step_id': 1, 'kind': 'branch'}
    assert _rows(store, entry['id']) == [*copied[2:], *own]

  def test_a_copy_of_a_copy_forks_from_the_trail_holding_the_last_copied_line(
    self, environment, store
  ):
    first = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    root = self._recorded(environment, store, 'seg-1', first)
    head = [json.dumps({'type': 'permission-mode', 'permissionMode': 'default'})]
    copied = _forked(first, 'seg-2')
    middle = self._recorded(environment, store, 'seg-2', [*head, *copied, _user('second', 'v1')])
    assert _entries('seg-2')[0]['forked_from'] == {'trail_id': root, 'step_id': 1, 'kind': 'branch'}
    # a branch of the second transcript at a line it copied from the first
    own = [_user('third', 'w1')]
    _write(environment, 'seg-3', [*_branched(copied, 'seg-2', 'seg-3'), *own])

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-3')
    assert entry['forked_from'] == {'trail_id': root, 'step_id': 1, 'kind': 'branch'}
    assert entry['spans'] == [[2, None]]
    assert _rows(store, entry['id']) == own
    assert _rows(store, middle) == [*head, _user('second', 'v1')]

  def _resumed(self, projects: Path, store: LocalStore, first: list[str], then: list[str]) -> str:
    """record `first` and then, in a later lifetime, `then` into one transcript,
    returning the first trail's id."""
    earlier = self._recorded(projects, store, 'seg-1', first)
    _write(projects, 'seg-1', first + then)
    _lifetime(projects, store)
    assert len(_entries('seg-1')) == 2
    return earlier

  def test_a_branch_at_an_earlier_trails_line_forks_from_that_trail(self, environment, store):
    first = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    earlier = self._resumed(environment, store, first, [_user('again', 'u2')])
    own = [_user('branched', 'u3')]
    _write(environment, 'seg-2', [*_branched(first, 'seg-1', 'seg-2'), *own])

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-2')
    assert entry['forked_from'] == {'trail_id': earlier, 'step_id': 1, 'kind': 'branch'}
    assert _rows(store, entry['id']) == own

  def test_an_earlier_trail_that_no_longer_verifies_gives_up_the_transcript(
    self, environment, store
  ):
    first = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    then = [_user('again', 'u2')]
    self._resumed(environment, store, first, then)
    changed = [first[0], _assistant('rewritten', 'a1')]
    _write(environment, 'seg-1', changed + then)
    copy = [*_branched(changed, 'seg-1', 'seg-2'), _user('branched', 'u3')]
    _write(environment, 'seg-2', copy)

    assert _recorder(environment, store).tick() is True

    assert _entries('seg-1') == []
    [entry] = _entries('seg-2')
    assert entry['forked_from'] is None
    assert _rows(store, entry['id']) == copy

  def test_a_copy_whose_origin_file_is_gone_is_a_root(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._recorded(environment, store, 'seg-1', lines)
    (environment / 'seg-1.jsonl').unlink()
    copy = [*_branched(lines, 'seg-1', 'seg-2'), _user('branched', 'u2')]
    _write(environment, 'seg-2', copy)

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-2')
    assert entry['forked_from'] is None
    assert _rows(store, entry['id']) == copy

  def test_a_copy_of_lines_no_trail_holds_is_a_root(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)
    store.refusals = 1
    recorder = _recorder(environment, store)
    with pytest.raises(RuntimeError, match='store is down'):
      recorder.tick()
    assert len(_entries('seg-1')) == 1
    copy = [*_branched(lines, 'seg-1', 'seg-2'), _user('branched', 'u2')]
    _write(environment, 'seg-2', copy)

    assert _recorder(environment, store).tick() is True

    # the origin's trail held no row: settling it left a transcript the map does not know
    assert _entries('seg-1') == []
    [entry] = _entries('seg-2')
    assert entry['forked_from'] is None
    assert _rows(store, entry['id']) == copy

  def test_a_cleared_conversation_is_a_root(self, environment, store):
    self._recorded(environment, store, 'seg-1', [_user('hello', 'u1'), _assistant('hi', 'a1')])
    cleared = [_user('fresh start', 'z1')]
    _write(environment, 'seg-2', cleared)

    assert _recorder(environment, store).tick() is True

    [entry] = _entries('seg-2')
    assert entry['forked_from'] is None
    assert _rows(store, entry['id']) == cleared


class TestSwitching:
  def test_each_stretch_of_a_lifetime_is_a_trail_of_its_own(self, environment, store):
    """the pinned Claude Code's resume, `/clear`, `/resume` and `/branch`, in turn."""
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-a', lines)
    _lifetime(environment, store)
    [earlier] = _entries('seg-a')
    recorder = _recorder(environment, store)

    # `ride resume` appends to the transcript
    resumed = [_user('resumed', 'u2'), _assistant('ok', 'a2')]
    _write(environment, 'seg-a', lines + resumed)
    assert recorder.tick() is True
    _, resume = _entries('seg-a')
    assert resume['forked_from'] == {'trail_id': earlier['id'], 'step_id': 1, 'kind': 'resume'}

    # `/clear` opens a transcript sharing no record
    cleared = [_user('cleared', 'z1')]
    _write(environment, 'seg-b', cleared)
    assert recorder.tick() is True
    assert _entries('seg-a')[-1]['ended'] is True
    assert store.get_trail(resume['id'])['end']['reason'] == 'ok'
    [root] = _entries('seg-b')
    assert root['forked_from'] is None
    assert trail_pointer.read(_pointer()) == root['id']

    # `/resume` back to the first transcript appends to it
    returned = [_user('back', 'u3')]
    _write(environment, 'seg-a', lines + resumed + returned)
    assert recorder.tick() is True
    assert _entries('seg-b')[-1]['ended'] is True
    _, _, back = _entries('seg-a')
    assert back['forked_from'] == {'trail_id': resume['id'], 'step_id': 1, 'kind': 'resume'}
    assert _rows(store, back['id']) == returned

    # `/branch` copies the first transcript into a new one
    whole = lines + resumed + returned
    own = [_user('branched', 'u4')]
    _write(environment, 'seg-c', [*_branched(whole, 'seg-a', 'seg-c'), *own])
    assert recorder.tick() is True
    [branch] = _entries('seg-c')
    assert branch['forked_from'] == {'trail_id': back['id'], 'step_id': 0, 'kind': 'branch'}
    assert _rows(store, branch['id']) == own

    assert recorder.finalize() is True
    assert _entries('seg-c')[-1]['ended'] is True
    assert trail_pointer.read(_pointer()) is None

  def test_switching_holds_while_the_followed_transcript_grows(self, environment, store):
    lines = [_user('hello', 'u1')]
    path = _write(environment, 'seg-1', lines)
    recorder = _recorder(environment, store, started_after=time.time() - 5)
    recorder.tick()

    # both files grow: the unrelated newer jsonl must not take the recording
    lines.append(_assistant('hi', 'a1'))
    path.write_text('\n'.join(lines) + '\n')
    _write(environment, 'seg-2', [_user('other', 'z1')])
    recorder.tick()

    [entry] = _entries('seg-1')
    assert store.get_trail(entry['id'])['end'] is None
    assert _rows(store, entry['id']) == lines
    assert _entries('seg-2') == []

  def test_a_transcript_that_shrank_ends_its_trail_and_loses_its_entries(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    recorder.tick()
    [first] = _entries('seg-1')
    rewritten = [_user('different', 'x1')]
    _write(environment, 'seg-1', rewritten)

    assert recorder.tick() is True
    assert store.get_trail(first['id'])['end']['reason'] == 'ok'
    assert _rows(store, first['id']) == lines
    assert _entries('seg-1') == []

    # what the transcript holds now is a transcript the map does not know
    assert recorder.tick() is True
    [root] = _entries('seg-1')
    assert root['forked_from'] is None
    assert _rows(store, root['id']) == rewritten


class TestStarts:
  def test_a_start_whose_reply_is_lost_is_answered_under_the_same_id(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    store.lost_replies = 1

    with pytest.raises(TransientUnavailable, match='reply lost'):
      recorder.tick()
    [entry] = _entries('seg-1')
    assert trail_pointer.read(_pointer()) is None

    assert recorder.tick() is True

    assert [request.id for request in store.starts] == [entry['id'], entry['id']]
    assert [header['id'] for header in store.iter_trails(harness='claude')] == [entry['id']]
    assert _entries('seg-1') == [entry]
    assert _rows(store, entry['id']) == lines
    assert trail_pointer.read(_pointer()) == entry['id']

  def test_a_start_whose_map_write_failed_is_retried_on_an_unchanged_transcript(
    self, environment, store, monkeypatch
  ):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    save = RecordingMap.save

    def fail_once(recording_map):
      monkeypatch.setattr(RecordingMap, 'save', save)
      raise OSError('map not writable')

    monkeypatch.setattr(RecordingMap, 'save', fail_once)
    with pytest.raises(OSError, match='map not writable'):
      recorder.tick()
    assert store.starts == []

    assert recorder.tick() is True

    [entry] = _entries('seg-1')
    assert [request.id for request in store.starts] == [entry['id']]
    assert _rows(store, entry['id']) == lines

  def test_a_collision_on_the_minted_id_ends_the_recording(self, environment, store, monkeypatch):
    _write(environment, 'taken', [_user('elsewhere', 'x1')])
    _recorder(environment, store).tick()
    [taken] = _entries('taken')
    (environment / 'taken.jsonl').unlink()
    _write(environment, 'seg-1', [_user('hello', 'u1')])
    monkeypatch.setattr('bro.trails.record.spine.lulid', lambda: taken['id'])
    recorder = _recorder(environment, store)

    with pytest.raises(TrailCollision):
      recorder.tick()
    starts = len(store.starts)
    with pytest.raises(TrailCollision):
      recorder.tick()
    with pytest.raises(TrailCollision):
      recorder.finalize()

    assert len(store.starts) == starts


class TestClose:
  def test_finalize_appends_ends_ok_and_clears_the_pointer(self, environment, store):
    lines = [_user('hello', 'u1')]
    path = _write(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    recorder.tick()
    lines.append(_assistant('bye', 'a1'))
    path.write_text('\n'.join(lines) + '\n')

    assert recorder.finalize() is True

    [entry] = _entries('seg-1')
    assert entry['ended'] is True
    end = store.get_trail(entry['id'])['end']
    assert end == {'at': end['at'], 'reason': 'ok'}
    assert _rows(store, entry['id']) == lines
    assert trail_pointer.read(_pointer()) is None

  def test_finalize_records_a_transcript_no_pass_followed_yet(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write(environment, 'seg-1', lines)

    assert _recorder(environment, store).finalize() is True

    [entry] = _entries('seg-1')
    assert entry['ended'] is True
    assert _rows(store, entry['id']) == lines

  def test_terminal_raise_ends_the_trail_raised(self, environment, store):
    path = _write(environment, 'seg-1', [_user('go', 'u1'), _raise_call('no api key')])
    worker = _worker(store, path)

    worker.close()

    end = store.get_trail(worker.trail_id)['end']
    assert (end['reason'], end['detail']) == ('raised', 'no api key')

  def test_the_interrupt_that_flushed_the_raise_turn_does_not_clear_it(self, environment, store):
    path = _write(
      environment,
      'seg-1',
      [_user('go', 'u1'), _raise_call('no api key'), _interrupt_notice()],
    )
    worker = _worker(store, path)

    worker.close()

    end = store.get_trail(worker.trail_id)['end']
    assert (end['reason'], end['detail']) == ('raised', 'no api key')

  def test_a_later_real_user_message_clears_the_raise(self, environment, store):
    path = _write(
      environment,
      'seg-1',
      [_user('go', 'u1'), _raise_call('stuck'), _user('resumed', 'u2')],
    )
    worker = _worker(store, path)

    worker.close()

    assert store.get_trail(worker.trail_id)['end']['reason'] == 'ok'

  def test_a_task_notification_does_not_clear_the_raise(self, environment, store):
    notification = _user(
      '<task-notification>done</task-notification>', 'u2', origin={'kind': 'task-notification'}
    )
    path = _write(environment, 'seg-1', [_user('go', 'u1'), _raise_call('stuck'), notification])
    worker = _worker(store, path)

    worker.close()

    end = store.get_trail(worker.trail_id)['end']
    assert (end['reason'], end['detail']) == ('raised', 'stuck')

  def test_a_prompt_typed_mid_turn_clears_the_raise(self, environment, store):
    queued = _record(
      type='attachment',
      uuid='q1',
      attachment={
        'type': 'queued_command',
        'prompt': 'resumed',
        'commandMode': 'prompt',
        'origin': {'kind': 'human'},
      },
    )
    path = _write(environment, 'seg-1', [_user('go', 'u1'), _raise_call('stuck'), queued])
    worker = _worker(store, path)

    worker.close()

    assert store.get_trail(worker.trail_id)['end']['reason'] == 'ok'

  def test_closing_a_rewound_transcript_ends_the_trail_without_appending(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    worker = _worker(store, _write(environment, 'seg-1', lines))
    worker.tick()
    _write(environment, 'seg-1', [_user('different', 'x1')])

    worker.close()

    assert store.get_trail(worker.trail_id)['end']['reason'] == 'ok'
    assert _rows(store, worker.trail_id) == lines

  def test_finalize_without_a_followed_transcript_is_a_noop(self, environment, store):
    recorder = _recorder(environment, store, started_after=time.time())

    assert recorder.finalize() is False
    assert store.starts == []


LOG = Path('/session/claude/session-recorder.log')


def _fail() -> bool:
  raise RuntimeError('store is down')


@pytest.fixture
def health_file(environment) -> Path:
  """the beat file, in the session state dir the environment fixture points at."""
  del environment
  path = health.health_path()
  assert path is not None
  return path


class TestHealthBeat:
  def test_a_quiet_attempt_beats_ok(self, environment, store, health_file):
    # an empty projects dir: the tick follows nothing and advances no trail
    _attempt(
      _recorder(environment, store, started_after=time.time()).tick, interval=3, log_path=LOG
    )

    assert json.loads(health_file.read_text())['status'] == 'ok'
    assert health.problem() is None

  def test_a_raising_attempt_beats_the_error(self, health_file):
    _attempt(_fail, interval=3, log_path=LOG)

    assert json.loads(health_file.read_text())['error'].endswith('store is down')
    assert health.problem() == f'FAILING — see {LOG}'

  def test_a_quiet_attempt_clears_an_earlier_error(self, environment, store, health_file):
    _attempt(_fail, interval=3, log_path=LOG)
    _attempt(
      _recorder(environment, store, started_after=time.time()).tick, interval=3, log_path=LOG
    )

    assert json.loads(health_file.read_text())['status'] == 'ok'
    assert health.problem() is None

  def test_the_shutdown_attempt_promises_no_further_beat(self, environment, store, health_file):
    _attempt(
      _recorder(environment, store, started_after=time.time()).finalize, interval=None, log_path=LOG
    )

    assert json.loads(health_file.read_text())['stale_after'] is None


def test_a_malformed_recording_map_ends_the_daemon_with_its_health(
  environment, health_file, tmp_path, monkeypatch
):
  from bro.claude import trail_recorder

  _session_dir().mkdir(parents=True, exist_ok=True)
  (_session_dir() / FILENAME).write_text('{"format": 2, "records": {}}')
  monkeypatch.setattr(trail_recorder, 'default_store', lambda: pytest.fail('store opened'))

  assert trail_recorder.record_session(environment, log_path=LOG) == 1

  beat = json.loads(health_file.read_text())
  assert beat['status'] == 'error'
  assert 'RecordingMapError' in beat['error']
  assert beat['stale_after'] is None


def test_missing_indexed_config_reports_health_without_requesting_a_material_path(
  environment, tmp_path, monkeypatch, caplog
):
  from bro.base import credentials
  from bro.claude import trail_recorder
  from ride.workspace.store import materialize_scoped_store

  source = credentials.Store(credentials.default_registry(), tmp_path / 'source', {})
  files, _ = credentials.build_scoped_store(source, [])
  store = credentials.Store(
    source.registry, materialize_scoped_store(files, tmp_path / 'scoped'), {}
  )
  events = []
  monkeypatch.setattr(trail_recorder, 'default_store', lambda: credentials.get_json('trails'))
  monkeypatch.setattr(
    trail_recorder.health, 'write', lambda *args, **kwargs: events.append((args, kwargs))
  )
  with credentials.as_default_store(store):
    assert trail_recorder.record_session(environment, log_path=tmp_path / 'log') == 1
  assert events[0][0][0] == 'error'
  assert 'config not found: trails' in caplog.text
