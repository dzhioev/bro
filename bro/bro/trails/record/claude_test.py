import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from session_log import trail_pointer
from session_log.recorder import Recorder, RecorderState, _fork_cuts, _scan_lines, _state_path


class FakeTrails:
  """in-memory stand-in for the TrailsClient surface the recorder drives."""

  def __init__(self):
    self.created: list[dict] = []
    self.headers: dict[str, dict] = {}
    self.artifacts: dict[str, str] = {}
    self.natives: dict[str, dict] = {}
    self.ends: dict[str, dict] = {}
    self.keepalives: list[str] = []
    self._counter = 0

  def create_trail(self, payload: dict) -> dict:
    self._counter += 1
    trail_id = f'T{self._counter}'
    self.created.append(payload)
    self.headers[trail_id] = {'id': trail_id, 'subject': payload.get('subject'), **payload}
    self.artifacts[trail_id] = payload['body']['artifact']
    self.natives[trail_id] = dict(payload['native'])
    return {'id': trail_id, 'started_at': '2026-01-01T00:00:00Z'}

  def replace_artifact(self, trail_id: str, artifact: str, native: dict) -> dict:
    self.artifacts[trail_id] = artifact
    self.natives[trail_id].update(native)
    return {'line_count': len(artifact.splitlines()), **native}

  def update_header(self, trail_id: str, changes: dict) -> dict:
    header = self.headers[trail_id]
    header.update(changes)
    return dict(header)

  def end_trail(self, trail_id: str, reason: str, detail: Optional[str] = None) -> None:
    self.ends[trail_id] = {'reason': reason, 'detail': detail}

  def keepalive(self, trail_id: str) -> None:
    self.keepalives.append(trail_id)

  def get_steps(self, trail_id: str, *, after: Optional[str] = None, limit: int = 100) -> dict:
    lines = self.artifacts[trail_id].splitlines()
    start = int(after) + 1 if after is not None else 0
    selected = lines[start : start + limit]
    steps = [
      {'trail_id': trail_id, 'step_id': str(index), 'raw': raw, 'record': _parse(raw)}
      for index, raw in enumerate(selected, start=start)
    ]
    next_cursor = str(start + len(selected) - 1) if start + len(selected) < len(lines) else None
    return {'steps': steps, 'next': next_cursor}

  def iter_steps(self, trail_id: str):
    after: Optional[str] = None
    while True:
      page = self.get_steps(trail_id, after=after)
      yield from page['steps']
      after = page.get('next')
      if after is None:
        return


def _parse(raw: str) -> Optional[dict]:
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return parsed if isinstance(parsed, dict) else None


def _record(**fields: Any) -> str:
  return json.dumps({'version': '2.1.216', **fields})


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


@pytest.fixture
def environment(tmp_path: Path, monkeypatch):
  config = tmp_path / 'config'
  projects = config / 'projects' / '-workspace'
  projects.mkdir(parents=True)
  monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
  monkeypatch.setenv('CW_COMMAND', 'cw ss ws')
  monkeypatch.setenv('CW_BRO', 'ppp-dev')
  monkeypatch.setenv('BRO_HOLD', 'attended')
  monkeypatch.setenv(
    'CW_SESSION_CONTEXT', json.dumps([{'title': 'git state', 'fields': {'branch': 'b'}}])
  )
  monkeypatch.delenv('CW_HOST', raising=False)
  monkeypatch.delenv('CW_HOST_WORKSPACE', raising=False)
  # the suite itself may run inside a container; pin the probe to host mode
  monkeypatch.setattr('session_log.recorder._in_container', lambda: False)
  return projects


def _recorder(projects: Path, fake: FakeTrails, *, started_after: float = 0.0) -> Recorder:
  return Recorder(
    projects,
    'ws',
    fake,  # type: ignore[arg-type] — structural stand-in for TrailsClient
    llm={'model': 'claude-fable-5'},
    cw_command=os.environ['CW_COMMAND'],
    started_after=started_after,
  )


def _write_segment(projects: Path, stem: str, lines: list[str]) -> Path:
  path = projects / f'{stem}.jsonl'
  path.write_text('\n'.join(lines) + '\n')
  return path


class TestAdoption:
  def test_fresh_segment_becomes_a_root_trail(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    assert recorder.tick() is True
    [payload] = fake.created
    assert payload['harness'] == 'claude'
    assert payload['surface'] == 'cw'
    assert payload['interactive'] is True
    assert payload['bro'] == 'ppp-dev'
    assert payload['hold'] == 'attended'
    assert 'forked_from' not in payload
    assert payload['native']['segment'] == 'seg-1'
    assert payload['native']['cw_command'] == 'cw ss ws'
    assert payload['native']['llm'] == {'model': 'claude-fable-5'}
    assert payload['body']['launch_context'] == [{'title': 'git state', 'fields': {'branch': 'b'}}]
    assert payload['location']['workspace'] == 'ws'
    assert payload['location']['is_container'] is False
    assert fake.artifacts['T1'] == '\n'.join(lines) + '\n'
    assert fake.natives['T1']['harness_version'] == '2.1.216'
    header = fake.headers['T1']
    assert header['turn_count'] == 1
    assert 'last_alive_at' in header

  def test_transcripts_older_than_the_launch_are_not_adopted(self, environment):
    projects = environment
    fake = FakeTrails()
    path = _write_segment(projects, 'seg-old', [_user('old', 'u1')])
    os.utime(path, (1, 1))
    recorder = _recorder(projects, fake, started_after=100.0)
    assert recorder.tick() is False
    assert fake.created == []

  def test_state_and_pointer_track_the_created_trail(self, environment):
    projects = environment
    fake = FakeTrails()
    _write_segment(projects, 'seg-1', [_user('hello', 'u1')])
    recorder = _recorder(projects, fake)
    recorder.tick()
    state = RecorderState.load(_state_path(projects))
    assert state is not None
    assert state.trail_id == 'T1'
    assert state.segment == 'seg-1'
    assert state.chunks == [[0, 1]]
    assert trail_pointer.read(trail_pointer.path()) == 'T1'

  def test_a_stale_pointer_is_cleared_at_start(self, environment):
    projects = environment
    trail_pointer.publish('STALE')
    _recorder(projects, FakeTrails())
    assert trail_pointer.read(trail_pointer.path()) is None


class TestSnapshots:
  def test_growth_re_puts_the_complete_suffix(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1')]
    path = _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    lines.append(_assistant('hi', 'a1'))
    path.write_text('\n'.join(lines) + '\n')
    assert recorder.tick() is True
    assert fake.artifacts['T1'] == '\n'.join(lines) + '\n'

  def test_quiet_tick_keepalives_after_the_idle_interval(self, environment):
    projects = environment
    fake = FakeTrails()
    _write_segment(projects, 'seg-1', [_user('hello', 'u1')])
    recorder = _recorder(projects, fake)
    recorder.tick()
    assert recorder.tick() is False
    assert fake.keepalives == []  # inside the idle window: no traffic
    recorder._last_write_monotonic = time.monotonic() - 120.0
    assert recorder.tick() is False
    assert fake.keepalives == ['T1']

  def test_usage_sums_dedup_claudes_split_records(self, environment):
    projects = environment
    fake = FakeTrails()
    usage = {'input_tokens': 5, 'output_tokens': 7}
    lines = [
      _assistant('a', 'a1', message_id='m1', usage=usage),
      _assistant('b', 'a2', message_id='m1', usage=usage),
      _assistant('c', 'a3', message_id='m2', usage=usage),
    ]
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    assert fake.natives['T1']['usage'] == {
      'claude-fable-5': {'input_tokens': 10, 'output_tokens': 14}
    }

  def test_turn_count_excludes_meta_and_tool_results(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [
      _user('real input', 'u1'),
      _user('injected', 'u2', isMeta=True),
      _record(
        type='user',
        uuid='u3',
        message={'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]},
      ),
      _user('another', 'u4'),
    ]
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    assert fake.headers['T1']['turn_count'] == 2

  def test_subject_initialized_from_ai_title_once(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1')]
    path = _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    assert fake.headers['T1'].get('subject') is None
    lines.append(json.dumps({'type': 'ai-title', 'aiTitle': 'first title'}))
    path.write_text('\n'.join(lines) + '\n')
    recorder.tick()
    assert fake.headers['T1']['subject'] == 'first title'
    # an explicit rename wins: later ticks never touch the subject again
    fake.headers['T1']['subject'] = 'my rename'
    lines.append(json.dumps({'type': 'ai-title', 'aiTitle': 'newer title'}))
    path.write_text('\n'.join(lines) + '\n')
    recorder.tick()
    assert fake.headers['T1']['subject'] == 'my rename'


class TestLifetimeForks:
  def _record_first_lifetime(self, projects, fake, lines) -> None:
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    recorder.finalize()

  def test_same_segment_resume_forks_from_the_final_line(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, lines)
    assert fake.ends['T1'] == {'reason': 'ok', 'detail': None}
    appended = [_user('again', 'u2')]
    _write_segment(projects, 'seg-1', lines + appended)
    second = _recorder(projects, fake)
    assert second.tick() is True
    fork = fake.created[-1]
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': '1'}
    assert fake.artifacts['T2'] == '\n'.join(appended) + '\n'

  def test_failed_anchors_start_a_fresh_root(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, lines)
    # the file was rewritten: the recorded extent no longer matches the trail
    rewritten = [_user('different', 'x1'), _user('content', 'x2'), _user('again', 'x3')]
    _write_segment(projects, 'seg-1', rewritten)
    second = _recorder(projects, fake)
    assert second.tick() is True
    root = fake.created[-1]
    assert 'forked_from' not in root
    assert fake.artifacts['T2'] == '\n'.join(rewritten) + '\n'

  def test_copied_history_resume_forks_and_skips_the_copy(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, lines)
    ephemera = json.dumps({'type': 'mode', 'mode': 'normal'})
    copy = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [ephemera, *copy, *tail])
    second = _recorder(projects, fake)
    assert second.tick() is True
    fork = fake.created[-1]
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': '1'}
    assert fork['native']['segment'] == 'seg-2'
    # only the new segment's own contribution: pre-copy ephemera + the tail
    assert fake.artifacts['T2'] == ephemera + '\n' + '\n'.join(tail) + '\n'

  def test_unverified_copy_starts_a_fresh_root(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, lines)
    # /clear: the new segment carries no history copy
    cleared = [_user('fresh start', 'z1')]
    _write_segment(projects, 'seg-2', cleared)
    second = _recorder(projects, fake)
    assert second.tick() is True
    root = fake.created[-1]
    assert 'forked_from' not in root
    assert fake.artifacts['T2'] == '\n'.join(cleared) + '\n'

  def test_copied_history_verifies_through_the_server_when_the_file_is_gone(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, lines)
    (projects / 'seg-1.jsonl').unlink()
    copy = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [*copy, *tail])
    second = _recorder(projects, fake)
    assert second.tick() is True
    fork = fake.created[-1]
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': '1'}

  def test_missing_state_starts_a_fresh_root(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, lines)
    _state_path(projects).unlink()
    _write_segment(projects, 'seg-1', lines + [_user('again', 'u2')])
    second = _recorder(projects, fake)
    assert second.tick() is True
    assert 'forked_from' not in fake.created[-1]


class TestTransitions:
  def test_segment_transition_closes_then_forks(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    # the leave→resume fork: a new segment carrying the history copy appears
    # while seg-1 is quiet
    copy = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [*copy, *tail])
    assert recorder.tick() is True
    assert fake.ends['T1'] == {'reason': 'ok', 'detail': None}
    fork = fake.created[-1]
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': '1'}
    assert trail_pointer.read(trail_pointer.path()) == 'T2'

  def test_transition_holds_while_the_active_segment_grows(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1')]
    path = _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake, started_after=time.time() - 5)
    recorder.tick()
    # both files grow: the unrelated newer jsonl must not steal the recording
    lines.append(_assistant('hi', 'a1'))
    path.write_text('\n'.join(lines) + '\n')
    _write_segment(projects, 'seg-2', [_user('other', 'z1')])
    recorder.tick()
    assert 'T1' not in fake.ends
    assert len(fake.created) == 1
    assert fake.artifacts['T1'] == '\n'.join(lines) + '\n'


class TestClose:
  def test_finalize_snapshots_ends_ok_and_clears_the_pointer(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1')]
    path = _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    lines.append(_assistant('bye', 'a1'))
    path.write_text('\n'.join(lines) + '\n')
    assert recorder.finalize() is True
    assert fake.ends['T1'] == {'reason': 'ok', 'detail': None}
    assert fake.artifacts['T1'] == '\n'.join(lines) + '\n'
    assert trail_pointer.read(trail_pointer.path()) is None

  def test_terminal_raise_ends_the_trail_raised(self, environment):
    projects = environment
    fake = FakeTrails()
    raise_call = _record(
      type='assistant',
      uuid='a1',
      message={
        'id': 'm1',
        'model': 'claude-fable-5',
        'usage': {'input_tokens': 1, 'output_tokens': 1},
        'content': [
          {'type': 'tool_use', 'name': 'mcp__bro__raise', 'input': {'reason': 'no api key'}}
        ],
      },
    )
    _write_segment(projects, 'seg-1', [_user('go', 'u1'), raise_call])
    recorder = _recorder(projects, fake)
    recorder.tick()
    recorder.finalize()
    assert fake.ends['T1'] == {'reason': 'raised', 'detail': 'no api key'}

  def test_a_later_real_user_message_clears_the_raise(self, environment):
    projects = environment
    fake = FakeTrails()
    raise_call = _record(
      type='assistant',
      uuid='a1',
      message={
        'id': 'm1',
        'model': 'claude-fable-5',
        'usage': {'input_tokens': 1, 'output_tokens': 1},
        'content': [{'type': 'tool_use', 'name': 'mcp__bro__raise', 'input': {'reason': 'stuck'}}],
      },
    )
    _write_segment(projects, 'seg-1', [_user('go', 'u1'), raise_call, _user('resumed', 'u2')])
    recorder = _recorder(projects, fake)
    recorder.tick()
    recorder.finalize()
    assert fake.ends['T1'] == {'reason': 'ok', 'detail': None}

  def test_finalize_without_an_adopted_segment_is_a_noop(self, environment):
    projects = environment
    fake = FakeTrails()
    recorder = _recorder(projects, fake, started_after=time.time())
    assert recorder.finalize() is False
    assert fake.created == []


class TestForkCuts:
  def test_verified_copy_locates_the_cut_lines(self):
    parent = [(0, 'u1'), (1, 'a1'), (2, 'u2')]
    new_lines = [
      json.dumps({'type': 'mode'}),
      json.dumps({'type': 'user', 'uuid': 'u1'}),
      json.dumps({'type': 'assistant', 'uuid': 'a1'}),
      json.dumps({'type': 'user', 'uuid': 'u2'}),
      json.dumps({'type': 'user', 'uuid': 'u3'}),
    ]
    cuts = _fork_cuts(parent, new_lines)
    assert cuts.verified is True
    assert cuts.copy_start_line == 1
    assert cuts.resume_start_line == 4
    assert cuts.anchor_index == 2

  def test_copy_may_drop_trailing_ephemera(self):
    # the last parent records are missing from the copy — still verified via
    # the recent tail, and the fork anchors at the last record actually found
    parent = [(0, 'u1'), (1, 'a1'), (2, 'e1')]
    new_lines = [
      json.dumps({'type': 'user', 'uuid': 'u1'}),
      json.dumps({'type': 'assistant', 'uuid': 'a1'}),
      json.dumps({'type': 'user', 'uuid': 'u2'}),
    ]
    cuts = _fork_cuts(parent, new_lines)
    assert cuts.verified is True
    assert cuts.anchor_index == 1
    assert cuts.resume_start_line == 2

  def test_missing_first_uuid_is_unverified(self):
    parent = [(0, 'u1'), (1, 'a1')]
    new_lines = [json.dumps({'type': 'assistant', 'uuid': 'a1'})]
    assert _fork_cuts(parent, new_lines).verified is False

  def test_stale_anchor_outside_the_recent_tail_is_unverified(self):
    # only an early uuid appears: an incomplete copy must not fork
    parent = [(index, f'u{index}') for index in range(40)]
    new_lines = [json.dumps({'type': 'user', 'uuid': 'u1'})]
    assert _fork_cuts(parent, new_lines).verified is False


class TestScan:
  def test_harness_version_and_title_extraction(self):
    scan = _scan_lines(
      [
        _user('hi', 'u1'),
        json.dumps({'type': 'ai-title', 'aiTitle': 'first'}),
        json.dumps({'type': 'ai-title', 'aiTitle': 'second'}),
        'not json',
      ]
    )
    assert scan.harness_version == '2.1.216'
    assert scan.ai_title == 'second'
    assert scan.turn_count == 1
