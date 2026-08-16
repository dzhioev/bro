import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from bro.monitor import trail_pointer
from bro.trails.local import LocalStore
from bro.trails.record.claude import Recorder


class _Store(LocalStore):
  """a real local store that also records the keepalives it was sent."""

  def __init__(self, root: Path):
    super().__init__(root)
    self.keepalives: list[str] = []

  def keepalive(self, trail_id: str) -> None:
    self.keepalives.append(trail_id)
    super().keepalive(trail_id)


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


_EPHEMERA = json.dumps({'type': 'mode', 'mode': 'normal'})


@pytest.fixture
def environment(tmp_path: Path, monkeypatch):
  config = tmp_path / 'config'
  projects = config / 'projects' / '-workspace'
  projects.mkdir(parents=True)
  monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
  monkeypatch.setenv('CW_COMMAND', 'cw ss ws')
  monkeypatch.setenv('CW_BRO', 'dev')
  monkeypatch.setenv('BRO_HOLD', 'attended')
  monkeypatch.setenv(
    'CW_SESSION_CONTEXT', json.dumps([{'title': 'git state', 'fields': {'branch': 'b'}}])
  )
  monkeypatch.delenv('CW_HOST', raising=False)
  monkeypatch.delenv('CW_HOST_WORKSPACE', raising=False)
  # the suite itself may run inside a container; pin the probe to host mode
  monkeypatch.setattr('bro.trails.record.claude._in_container', lambda: False)
  return projects


@pytest.fixture
def store(tmp_path: Path) -> _Store:
  return _Store(tmp_path / 'trails')


def _recorder(projects: Path, store: LocalStore, *, started_after: float = 0.0) -> Recorder:
  return Recorder(
    projects,
    'ws',
    store,
    llm={'model': 'claude-fable-5'},
    cw_command=os.environ['CW_COMMAND'],
    started_after=started_after,
  )


def _write_segment(projects: Path, stem: str, lines: list[str]) -> Path:
  path = projects / f'{stem}.jsonl'
  path.write_text('\n'.join(lines) + '\n')
  return path


def _trails(store: LocalStore) -> list[dict]:
  """recorded headers, oldest first."""
  return sorted(
    store.iter_trails(harness='claude'), key=lambda header: (header['started_at'], header['id'])
  )


def _rows(store: LocalStore, trail_id: str) -> list[str]:
  return [step['body'] for step in store.iter_steps(trail_id)]


class TestAdoption:
  def test_fresh_segment_becomes_a_root_trail(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write_segment(environment, 'seg-1', lines)

    assert _recorder(environment, store).tick() is True

    [header] = _trails(store)
    assert header['harness'] == 'claude'
    assert header['surface'] == 'cw'
    assert header['interactive'] is True
    assert header['bro'] == 'dev'
    assert header['hold'] == 'attended'
    assert 'forked_from' not in header
    assert header['native']['segment'] == 'seg-1'
    assert header['native']['cw_command'] == 'cw ss ws'
    assert header['native']['llm'] == {'model': 'claude-fable-5'}
    assert header['location']['workspace'] == 'ws'
    assert header['location']['is_container'] is False
    assert store.get_launch_context(header['id']) == [
      {'title': 'git state', 'fields': {'branch': 'b'}}
    ]
    assert _rows(store, header['id']) == lines

  def test_transcripts_older_than_the_launch_are_not_adopted(self, environment, store):
    path = _write_segment(environment, 'seg-old', [_user('old', 'u1')])
    os.utime(path, (1, 1))

    assert _recorder(environment, store, started_after=100.0).tick() is False
    assert _trails(store) == []

  def test_a_segment_of_bare_ephemera_is_not_adopted(self, environment, store):
    _write_segment(environment, 'seg-1', [_EPHEMERA])
    recorder = _recorder(environment, store)

    assert recorder.tick() is False
    assert recorder.finalize() is False
    assert _trails(store) == []

  def test_the_pointer_tracks_the_created_trail(self, environment, store):
    _write_segment(environment, 'seg-1', [_user('hello', 'u1')])

    _recorder(environment, store).tick()

    [header] = _trails(store)
    assert trail_pointer.read(trail_pointer.path()) == header['id']

  def test_a_stale_pointer_is_cleared_at_start(self, environment, store):
    trail_pointer.publish('STALE')

    _recorder(environment, store)

    assert trail_pointer.read(trail_pointer.path()) is None

  def test_a_declined_segment_is_not_re_offered_until_it_changes(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write_segment(environment, 'seg-1', lines)
    first = _recorder(environment, store)
    first.tick()
    first.finalize()
    second = _recorder(environment, store)

    assert second.tick() is False  # nothing past the recorded extent yet
    blazes = len(_trails(store))
    assert second.tick() is False
    assert len(_trails(store)) == blazes


class TestAppends:
  def test_growth_appends_only_new_lines(self, environment, store):
    lines = [_user('hello', 'u1')]
    path = _write_segment(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    recorder.tick()
    lines.append(_assistant('hi', 'a1'))
    path.write_text('\n'.join(lines) + '\n')

    assert recorder.tick() is True

    [header] = _trails(store)
    assert _rows(store, header['id']) == lines

  def test_incomplete_line_waits_for_its_newline(self, environment, store):
    first = _user('hello', 'u1')
    path = _write_segment(environment, 'seg-1', [first])
    recorder = _recorder(environment, store)
    recorder.tick()
    second = _assistant('hi', 'a1')
    path.write_text(first + '\n' + second)

    assert recorder.tick() is False
    [header] = _trails(store)
    assert _rows(store, header['id']) == [first]

    path.write_text(first + '\n' + second + '\n')
    assert recorder.tick() is True
    assert _rows(store, header['id']) == [first, second]

  def test_quiet_tick_keepalives_after_the_idle_interval(self, environment, store):
    _write_segment(environment, 'seg-1', [_user('hello', 'u1')])
    recorder = _recorder(environment, store)
    recorder.tick()

    assert recorder.tick() is False
    assert store.keepalives == []  # inside the idle window: no traffic

    assert recorder._recording is not None
    recorder._recording._last_write_monotonic = time.monotonic() - 120.0
    assert recorder.tick() is False
    assert store.keepalives == [_trails(store)[0]['id']]


class TestLifetimeForks:
  def _first_lifetime(self, projects: Path, store: LocalStore, lines: list[str]) -> str:
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, store)
    recorder.tick()
    recorder.finalize()
    return _trails(store)[0]['id']

  def test_same_segment_resume_forks_from_the_final_line(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    first_id = self._first_lifetime(environment, store, lines)
    assert store.get_trail(first_id)['end']['reason'] == 'ok'
    appended = [_user('again', 'u2')]
    _write_segment(environment, 'seg-1', lines + appended)

    # a second lifetime shares nothing with the first but the store
    assert _recorder(environment, store).tick() is True

    fork = _trails(store)[-1]
    assert fork['forked_from'] == {'trail_id': first_id, 'step_id': 1}
    assert _rows(store, fork['id']) == appended

  def test_a_rewritten_segment_starts_a_fresh_root(self, environment, store):
    self._first_lifetime(environment, store, [_user('hello', 'u1'), _assistant('hi', 'a1')])
    rewritten = [_user('different', 'x1'), _user('content', 'x2'), _user('again', 'x3')]
    _write_segment(environment, 'seg-1', rewritten)

    assert _recorder(environment, store).tick() is True

    root = _trails(store)[-1]
    assert 'forked_from' not in root
    assert _rows(store, root['id']) == rewritten

  def test_copied_history_resume_forks_and_skips_the_copy(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    first_id = self._first_lifetime(environment, store, lines)
    tail = [_user('resumed', 'u2')]
    _write_segment(environment, 'seg-2', [_EPHEMERA, *lines, *tail])

    assert _recorder(environment, store).tick() is True

    fork = _trails(store)[-1]
    assert fork['forked_from'] == {'trail_id': first_id, 'step_id': 1}
    assert fork['native']['segment'] == 'seg-2'
    # only the new segment's own contribution: pre-copy ephemera + the tail
    assert _rows(store, fork['id']) == [_EPHEMERA, *tail]

  def test_a_cleared_conversation_starts_a_fresh_root(self, environment, store):
    self._first_lifetime(environment, store, [_user('hello', 'u1'), _assistant('hi', 'a1')])
    # /clear: the new segment carries no history copy
    cleared = [_user('fresh start', 'z1')]
    _write_segment(environment, 'seg-2', cleared)

    assert _recorder(environment, store).tick() is True

    root = _trails(store)[-1]
    assert 'forked_from' not in root
    assert _rows(store, root['id']) == cleared

  def test_adoption_waits_until_the_history_copy_is_written(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    first_id = self._first_lifetime(environment, store, lines)
    recorder = _recorder(environment, store)

    # the forked segment arrives in stages: head ephemera, then the history
    # copy record by record, then the resumed conversation
    _write_segment(environment, 'seg-2', [_EPHEMERA])
    assert recorder.tick() is False
    _write_segment(environment, 'seg-2', [_EPHEMERA, lines[0]])
    assert recorder.tick() is False
    _write_segment(environment, 'seg-2', [_EPHEMERA, *lines])
    assert recorder.tick() is False
    assert len(_trails(store)) == 1

    tail = [_user('resumed', 'u2')]
    _write_segment(environment, 'seg-2', [_EPHEMERA, *lines, *tail])
    assert recorder.tick() is True

    fork = _trails(store)[-1]
    assert fork['forked_from'] == {'trail_id': first_id, 'step_id': 1}
    assert _rows(store, fork['id']) == [_EPHEMERA, *tail]

  def test_a_copy_without_its_origin_file_starts_a_root(self, environment, store):
    """the sibling transcript is what names the segment a copy came from; with
    it gone there is nothing to scope the lookup to."""
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._first_lifetime(environment, store, lines)
    (environment / 'seg-1.jsonl').unlink()
    _write_segment(environment, 'seg-2', [*lines, _user('resumed', 'u2')])

    assert _recorder(environment, store).tick() is True

    assert 'forked_from' not in _trails(store)[-1]

  def test_copied_history_skips_records_the_chain_already_stores(self, environment, store):
    root = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._first_lifetime(environment, store, root)
    appended = [_user('again', 'u2')]
    _write_segment(environment, 'seg-1', root + appended)
    second = _recorder(environment, store)
    second.tick()
    second.finalize()
    second_id = _trails(store)[-1]['id']

    # the leave→resume copy re-serializes the whole conversation, root
    # lifetime included; only the new segment's own lines may be stored
    tail = [_user('third', 'u3')]
    _write_segment(environment, 'seg-2', [_EPHEMERA, *root, *appended, *tail])

    assert _recorder(environment, store).tick() is True

    fork = _trails(store)[-1]
    assert fork['forked_from'] == {'trail_id': second_id, 'step_id': 0}
    assert _rows(store, fork['id']) == [_EPHEMERA, *tail]


class TestTransitions:
  def test_segment_transition_closes_then_forks(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write_segment(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    recorder.tick()
    first_id = _trails(store)[0]['id']

    # the leave→resume fork: a new segment carrying the history copy appears
    # while seg-1 is quiet
    _write_segment(environment, 'seg-2', [*lines, _user('resumed', 'u2')])
    assert recorder.tick() is True

    assert store.get_trail(first_id)['end']['reason'] == 'ok'
    fork = _trails(store)[-1]
    assert fork['forked_from'] == {'trail_id': first_id, 'step_id': 1}
    assert trail_pointer.read(trail_pointer.path()) == fork['id']

  def test_transition_defers_adoption_until_the_copy_lands(self, environment, store):
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write_segment(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    recorder.tick()
    first_id = _trails(store)[0]['id']

    # the forked segment appears with its head ephemera only: seg-1 is over,
    # but the lineage the new one continues is not visible yet
    _write_segment(environment, 'seg-2', [_EPHEMERA])
    assert recorder.tick() is True
    assert store.get_trail(first_id)['end']['reason'] == 'ok'
    assert len(_trails(store)) == 1

    tail = [_user('resumed', 'u2')]
    _write_segment(environment, 'seg-2', [_EPHEMERA, *lines, *tail])
    assert recorder.tick() is True

    fork = _trails(store)[-1]
    assert fork['forked_from'] == {'trail_id': first_id, 'step_id': 1}
    assert _rows(store, fork['id']) == [_EPHEMERA, *tail]

  def test_transition_holds_while_the_active_segment_grows(self, environment, store):
    lines = [_user('hello', 'u1')]
    path = _write_segment(environment, 'seg-1', lines)
    recorder = _recorder(environment, store, started_after=time.time() - 5)
    recorder.tick()

    # both files grow: the unrelated newer jsonl must not steal the recording
    lines.append(_assistant('hi', 'a1'))
    path.write_text('\n'.join(lines) + '\n')
    _write_segment(environment, 'seg-2', [_user('other', 'z1')])
    recorder.tick()

    [header] = _trails(store)
    assert header['end'] is None
    assert _rows(store, header['id']) == lines


class TestClose:
  def test_finalize_appends_ends_ok_and_clears_the_pointer(self, environment, store):
    lines = [_user('hello', 'u1')]
    path = _write_segment(environment, 'seg-1', lines)
    recorder = _recorder(environment, store)
    recorder.tick()
    lines.append(_assistant('bye', 'a1'))
    path.write_text('\n'.join(lines) + '\n')

    assert recorder.finalize() is True

    [header] = _trails(store)
    assert store.get_trail(header['id'])['end'] == {
      'at': store.get_trail(header['id'])['end']['at'],
      'reason': 'ok',
    }
    assert _rows(store, header['id']) == lines
    assert trail_pointer.read(trail_pointer.path()) is None

  def test_terminal_raise_ends_the_trail_raised(self, environment, store):
    _write_segment(environment, 'seg-1', [_user('go', 'u1'), _raise_call('no api key')])
    recorder = _recorder(environment, store)
    recorder.tick()
    recorder.finalize()

    end = store.get_trail(_trails(store)[0]['id'])['end']
    assert (end['reason'], end['detail']) == ('raised', 'no api key')

  def test_a_later_real_user_message_clears_the_raise(self, environment, store):
    _write_segment(
      environment,
      'seg-1',
      [_user('go', 'u1'), _raise_call('stuck'), _user('resumed', 'u2')],
    )
    recorder = _recorder(environment, store)
    recorder.tick()
    recorder.finalize()

    assert store.get_trail(_trails(store)[0]['id'])['end']['reason'] == 'ok'

  def test_finalize_without_an_adopted_segment_is_a_noop(self, environment, store):
    recorder = _recorder(environment, store, started_after=time.time())

    assert recorder.finalize() is False
    assert _trails(store) == []
