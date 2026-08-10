import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from bro.cw.constants import CW_RESUMED_SESSION_ENV
from bro.monitor import trail_pointer
from bro.trails.record.claude import Recorder, RecorderState, _fork_cuts, _state_path


class FakeTrails:
  """in-memory stand-in for the TrailsStore surface the recorder drives."""

  def __init__(self):
    self.created: list[dict] = []
    self.headers: dict[str, dict] = {}
    self.artifacts: dict[str, str] = {}
    self.appends: list[dict] = []
    self.ends: dict[str, dict] = {}
    self.keepalives: list[str] = []
    self.uuid_lookups: list[set[str]] = []
    self.point_reads: list[tuple[str, int]] = []
    self.uuid_projections: list[tuple[str, Optional[int]]] = []
    self.history_scans = 0
    self._counter = 0

  def create_trail(self, payload: dict) -> dict:
    self._counter += 1
    trail_id = f'T{self._counter}'
    self.created.append(payload)
    started_at = f'2026-01-01T00:00:{self._counter:02d}Z'
    self.headers[trail_id] = {
      'id': trail_id,
      'started_at': started_at,
      'subject': payload.get('subject'),
      'body_storage': 'trail_steps_v2',
      **payload,
    }
    records = payload['body']['records']
    self.artifacts[trail_id] = ''.join(f'{record}\n' for record in records)
    return {'id': trail_id, 'started_at': started_at}

  def append_records(self, trail_id: str, offset: int, records: list[str]) -> dict:
    current = self.artifacts[trail_id].splitlines()
    expected_end = offset + len(records)
    if len(current) != offset:
      if len(current) == expected_end and current[offset:expected_end] == records:
        return {'extent': len(current), 'appended': 0, 'duplicate': True}
      raise ValueError(f'append offset {offset} does not match extent {len(current)}')
    self.appends.append({'trail_id': trail_id, 'offset': offset, 'records': list(records)})
    self.artifacts[trail_id] += ''.join(f'{record}\n' for record in records)
    return {'extent': expected_end, 'appended': len(records)}

  def end_trail(self, trail_id: str, reason: str, detail: Optional[str] = None) -> None:
    self.ends[trail_id] = {'reason': reason, 'detail': detail}

  def keepalive(self, trail_id: str) -> None:
    self.keepalives.append(trail_id)

  def find_steps_by_uuid(self, uuids: set[str]) -> list[dict]:
    self.uuid_lookups.append(uuids)
    return [
      {'trail_id': trail_id, 'step_id': step_id, 'uuid': record['uuid']}
      for trail_id, artifact in self.artifacts.items()
      for step_id, raw in enumerate(artifact.splitlines())
      if (record := _parse(raw)) is not None and record.get('uuid') in uuids
    ]

  def get_step(self, trail_id: str, step_id: int) -> dict:
    self.point_reads.append((trail_id, step_id))
    steps = self.get_steps(
      trail_id,
      after=step_id - 1 if step_id > 0 else None,
      limit=1,
    )['steps']
    return steps[0] if len(steps) > 0 else {}

  def get_step_uuids(self, trail_id: str, *, through: Optional[int] = None) -> list[dict]:
    self.uuid_projections.append((trail_id, through))
    rows: list[dict] = []
    for step_id, raw in enumerate(self.artifacts[trail_id].splitlines()):
      if through is not None and step_id > through:
        break
      record = _parse(raw)
      uuid = record.get('uuid') if record is not None else None
      if isinstance(uuid, str):
        rows.append({'step_id': step_id, 'uuid': uuid})
    return rows

  def get_steps(self, trail_id: str, *, after: Optional[int] = None, limit: int = 100) -> dict:
    lines = self.artifacts[trail_id].splitlines()
    start = after + 1 if after is not None else 0
    selected = lines[start : start + limit]
    steps = [
      {
        'trail_id': trail_id,
        'step_id': index,
        'raw': raw,
        'record': _parse(raw),
      }
      for index, raw in enumerate(selected, start=start)
    ]
    next_cursor = start + len(selected) - 1 if start + len(selected) < len(lines) else None
    return {'steps': steps, 'next': next_cursor}

  def get_trail(self, trail_id: str) -> dict:
    header = dict(self.headers[trail_id])
    header['extent'] = len(self.artifacts[trail_id].splitlines())
    return header

  def iter_trails(self, *, harness: str, since: Optional[str] = None):
    self.history_scans += 1
    del harness, since
    for trail_id in reversed(list(self.headers)):
      yield self.get_trail(trail_id)

  def iter_steps(self, trail_id: str):
    after: Optional[int] = None
    while True:
      page = self.get_steps(trail_id, after=after)
      yield from page['steps']
      after = page.get('next')
      if after is None:
        return

  def iter_messages(self, trail_id: str, *, types: Optional[set[str]] = None):
    for raw in self.artifacts[trail_id].splitlines():
      record = _parse(raw)
      if record is None:
        continue
      message = record.get('message')
      if record.get('type') == 'assistant' and isinstance(message, dict):
        content = message.get('content')
        if isinstance(content, list):
          for block in content:
            if isinstance(block, dict) and block.get('type') == 'tool_use':
              event = {
                'type': 'tool_call',
                'tool_name': block.get('name'),
                'arguments': block.get('input'),
              }
              if types is None or event['type'] in types:
                yield event
      elif record.get('type') == 'user' and isinstance(message, dict):
        content = message.get('content')
        tool_results_only = isinstance(content, list) and all(
          isinstance(block, dict) and block.get('type') == 'tool_result' for block in content
        )
        if not tool_results_only:
          event = {'type': 'user_input', 'content': content, 'isMeta': record.get('isMeta', False)}
          if types is None or event['type'] in types:
            yield event


def _parse(raw: str) -> Optional[dict]:
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return parsed if isinstance(parsed, dict) else None


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


@pytest.fixture
def environment(tmp_path: Path, monkeypatch):
  config = tmp_path / 'config'
  projects = config / 'projects' / '-workspace'
  projects.mkdir(parents=True)
  monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
  monkeypatch.setenv('CW_COMMAND', 'cw ss ws')
  monkeypatch.setenv('CW_BRO', 'dev')
  monkeypatch.setenv('BRO_HOLD', 'attended')
  monkeypatch.delenv(CW_RESUMED_SESSION_ENV, raising=False)
  monkeypatch.setenv(
    'CW_SESSION_CONTEXT', json.dumps([{'title': 'git state', 'fields': {'branch': 'b'}}])
  )
  monkeypatch.delenv('CW_HOST', raising=False)
  monkeypatch.delenv('CW_HOST_WORKSPACE', raising=False)
  # the suite itself may run inside a container; pin the probe to host mode
  monkeypatch.setattr('bro.trails.record.claude._in_container', lambda: False)
  return projects


def _recorder(projects: Path, fake: FakeTrails, *, started_after: float = 0.0) -> Recorder:
  return Recorder(
    projects,
    'ws',
    fake,  # type: ignore[arg-type] — structural stand-in for TrailsStore
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
    assert payload['bro'] == 'dev'
    assert payload['hold'] == 'attended'
    assert 'forked_from' not in payload
    assert payload['native']['segment'] == 'seg-1'
    assert payload['native']['cw_command'] == 'cw ss ws'
    assert payload['native']['llm'] == {'model': 'claude-fable-5'}
    assert payload['body']['records'] == []
    assert payload['body']['launch_context'] == [{'title': 'git state', 'fields': {'branch': 'b'}}]
    assert payload['location']['workspace'] == 'ws'
    assert payload['location']['is_container'] is False
    assert fake.appends == [{'trail_id': 'T1', 'offset': 0, 'records': lines}]
    assert fake.artifacts['T1'] == '\n'.join(lines) + '\n'

  def test_transcripts_older_than_the_launch_are_not_adopted(self, environment):
    projects = environment
    fake = FakeTrails()
    path = _write_segment(projects, 'seg-old', [_user('old', 'u1')])
    os.utime(path, (1, 1))
    recorder = _recorder(projects, fake, started_after=100.0)
    assert recorder.tick() is False
    assert fake.created == []

  def test_a_segment_of_bare_ephemera_is_not_adopted(self, environment):
    projects = environment
    fake = FakeTrails()
    _write_segment(projects, 'seg-1', [json.dumps({'type': 'mode', 'mode': 'normal'})])
    recorder = _recorder(projects, fake)
    assert recorder.tick() is False
    assert recorder.finalize() is False
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


class TestAppends:
  def test_growth_appends_only_new_lines(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1')]
    path = _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    appended = _assistant('hi', 'a1')
    lines.append(appended)
    path.write_text('\n'.join(lines) + '\n')
    assert recorder.tick() is True
    assert fake.appends[-1] == {'trail_id': 'T1', 'offset': 1, 'records': [appended]}
    assert fake.artifacts['T1'] == '\n'.join(lines) + '\n'

  def test_incomplete_line_waits_for_its_newline(self, environment):
    projects = environment
    fake = FakeTrails()
    first = _user('hello', 'u1')
    path = _write_segment(projects, 'seg-1', [first])
    recorder = _recorder(projects, fake)
    recorder.tick()
    second = _assistant('hi', 'a1')
    path.write_text(first + '\n' + second)
    assert recorder.tick() is False
    assert len(fake.appends) == 1
    path.write_text(first + '\n' + second + '\n')
    assert recorder.tick() is True
    assert fake.appends[-1] == {'trail_id': 'T1', 'offset': 1, 'records': [second]}

  def test_quiet_tick_keepalives_after_the_idle_interval(self, environment):
    projects = environment
    fake = FakeTrails()
    _write_segment(projects, 'seg-1', [_user('hello', 'u1')])
    recorder = _recorder(projects, fake)
    recorder.tick()
    assert recorder.tick() is False
    assert fake.keepalives == []  # inside the idle window: no traffic
    assert recorder._recording is not None
    recorder._recording._last_write_monotonic = time.monotonic() - 120.0
    assert recorder.tick() is False
    assert fake.keepalives == ['T1']


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
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
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
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
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

  def test_adoption_waits_until_the_history_copy_is_written(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, lines)
    ephemera = json.dumps({'type': 'mode', 'mode': 'normal'})
    second = _recorder(projects, fake)
    # the forked segment arrives in stages: head ephemera, then the history
    # copy record by record, then the resumed conversation
    _write_segment(projects, 'seg-2', [ephemera])
    assert second.tick() is False
    _write_segment(projects, 'seg-2', [ephemera, _user('hello', 'u1')])
    assert second.tick() is False
    _write_segment(projects, 'seg-2', [ephemera, *lines])
    assert second.tick() is False
    assert len(fake.created) == 1
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [ephemera, *lines, *tail])
    assert second.tick() is True
    fork = fake.created[-1]
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
    assert fake.artifacts['T2'] == ephemera + '\n' + '\n'.join(tail) + '\n'

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
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': 1}

  def test_a_new_segment_without_a_recorded_chain_starts_a_fresh_root(self, environment):
    projects = environment
    fake = FakeTrails()
    _write_segment(projects, 'seg-1', [_user('hello', 'u1')])
    recorder = _recorder(projects, fake)
    recorder.tick()
    assert 'forked_from' not in fake.created[-1]

  def test_copied_history_skips_records_the_chain_already_stores(self, environment):
    projects = environment
    fake = FakeTrails()
    root = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._record_first_lifetime(projects, fake, root)
    appended = [_user('again', 'u2')]
    _write_segment(projects, 'seg-1', root + appended)
    second = _recorder(projects, fake)
    second.tick()
    second.finalize()
    # the leave→resume copy re-serializes the whole conversation, root
    # lifetime included; only the new segment's own lines may be stored
    ephemera = json.dumps({'type': 'mode', 'mode': 'normal'})
    tail = [_user('third', 'u3')]
    _write_segment(projects, 'seg-2', [ephemera, *root, *appended, *tail])
    third = _recorder(projects, fake)
    assert third.tick() is True
    fork = fake.created[-1]
    assert fork['forked_from'] == {'trail_id': 'T2', 'step_id': 0}
    assert fake.artifacts['T3'] == ephemera + '\n' + '\n'.join(tail) + '\n'
    assert ('T1', 1) in fake.uuid_projections


class TestRecordedChainRecovery:
  def _recorded_root(self, projects, fake, lines) -> None:
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    recorder.finalize()

  def test_missing_state_continues_the_trail_recording_the_segment(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._recorded_root(projects, fake, lines)
    lookups_before_resume = len(fake.uuid_lookups)
    _state_path(projects).unlink()  # a session recorded before this daemon
    appended = [_user('again', 'u2')]
    _write_segment(projects, 'seg-1', lines + appended)
    second = _recorder(projects, fake)
    assert second.tick() is True
    assert fake.created[-1]['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
    assert fake.artifacts['T2'] == '\n'.join(appended) + '\n'
    assert fake.history_scans == 0
    assert len(fake.uuid_lookups) == lookups_before_resume + 1
    assert fake.point_reads[-2:] == [('T1', 0), ('T1', 1)]

  def test_missing_state_forks_a_copied_history_from_the_origin_segment(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._recorded_root(projects, fake, lines)
    _state_path(projects).unlink()
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [*lines, *tail])
    second = _recorder(projects, fake)
    assert second.tick() is True
    assert fake.created[-1]['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
    assert fake.artifacts['T2'] == '\n'.join(tail) + '\n'

  def test_declared_segment_selects_its_trail_before_inference(self, environment, monkeypatch):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._recorded_root(projects, fake, lines)
    _state_path(projects).unlink()
    _write_segment(projects, 'other-segment', lines)
    other_payload = dict(fake.created[0])
    other_payload['native'] = {**other_payload['native'], 'segment': 'other-segment'}
    other_id = fake.create_trail(other_payload)['id']
    fake.append_records(other_id, 0, lines)

    monkeypatch.setenv(CW_RESUMED_SESSION_ENV, 'seg-1')
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [*lines, *tail])
    second = _recorder(projects, fake)
    assert second.tick() is True
    assert fake.created[-1]['forked_from'] == {'trail_id': 'T1', 'step_id': 1}

  def test_malformed_uuid_lookup_fails_fast(self, environment):
    projects = environment
    fake = FakeTrails()
    _write_segment(projects, 'seg-1', [_user('hello', 'u1')])
    fake.find_steps_by_uuid = lambda uuids: [  # type: ignore[method-assign]
      {'trail_id': 'T1', 'step_id': 'bad', 'uuid': next(iter(uuids))}
    ]

    with pytest.raises(ValueError, match='malformed UUID lookup result'):
      _recorder(projects, fake).tick()

  def test_bad_declaration_falls_back_to_inferred_lineage(self, environment, monkeypatch):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._recorded_root(projects, fake, lines)
    _state_path(projects).unlink()
    monkeypatch.setenv(CW_RESUMED_SESSION_ENV, 'missing-segment')
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [*lines, *tail])

    second = _recorder(projects, fake)
    assert second.tick() is True
    assert fake.created[-1]['forked_from'] == {'trail_id': 'T1', 'step_id': 1}

  def test_state_that_outgrew_the_stored_artifact_recovers_the_real_extent(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._recorded_root(projects, fake, lines)
    # the artifact was trimmed under the daemon: the saved chunks now cover
    # lines the trail no longer stores
    fake.artifacts['T1'] = lines[1] + '\n'
    appended = [_user('again', 'u2')]
    _write_segment(projects, 'seg-1', lines + appended)
    second = _recorder(projects, fake)
    assert second.tick() is True
    assert fake.created[-1]['forked_from'] == {'trail_id': 'T1', 'step_id': 0}
    assert fake.artifacts['T2'] == '\n'.join(appended) + '\n'

  def test_state_beyond_the_transcript_length_recovers_the_real_extent(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    self._recorded_root(projects, fake, lines)
    # local state claiming more lines than the transcript holds — a transcript
    # that shrank under the daemon rather than grew
    RecorderState(trail_id='T1', segment='seg-1', chunks=[[0, 5]]).save(_state_path(projects))
    appended = [_user('again', 'u2')]
    _write_segment(projects, 'seg-1', lines + appended)
    second = _recorder(projects, fake)
    assert second.tick() is True
    assert fake.created[-1]['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
    assert fake.artifacts['T2'] == '\n'.join(appended) + '\n'


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
    assert fork['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
    assert trail_pointer.read(trail_pointer.path()) == 'T2'

  def test_transition_defers_adoption_until_the_copy_lands(self, environment):
    projects = environment
    fake = FakeTrails()
    lines = [_user('hello', 'u1'), _assistant('hi', 'a1')]
    _write_segment(projects, 'seg-1', lines)
    recorder = _recorder(projects, fake)
    recorder.tick()
    # the forked segment appears with its head ephemera only: seg-1 is over,
    # but the lineage the new one continues is not visible yet
    ephemera = json.dumps({'type': 'mode', 'mode': 'normal'})
    _write_segment(projects, 'seg-2', [ephemera])
    assert recorder.tick() is True
    assert fake.ends['T1'] == {'reason': 'ok', 'detail': None}
    assert len(fake.created) == 1
    tail = [_user('resumed', 'u2')]
    _write_segment(projects, 'seg-2', [ephemera, *lines, *tail])
    assert recorder.tick() is True
    assert fake.created[-1]['forked_from'] == {'trail_id': 'T1', 'step_id': 1}
    assert fake.artifacts['T2'] == ephemera + '\n' + '\n'.join(tail) + '\n'

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
  def test_finalize_appends_ends_ok_and_clears_the_pointer(self, environment):
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
    cuts = _fork_cuts(parent, set(), new_lines)
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
    cuts = _fork_cuts(parent, set(), new_lines)
    assert cuts.verified is True
    assert cuts.anchor_index == 1
    assert cuts.resume_start_line == 2

  def test_missing_first_uuid_is_unverified(self):
    parent = [(0, 'u1'), (1, 'a1')]
    new_lines = [json.dumps({'type': 'assistant', 'uuid': 'a1'})]
    assert _fork_cuts(parent, set(), new_lines).verified is False

  def test_stale_anchor_outside_the_recent_tail_is_unverified(self):
    # only an early uuid appears: an incomplete copy must not fork
    parent = [(index, f'u{index}') for index in range(40)]
    new_lines = [json.dumps({'type': 'user', 'uuid': 'u1'})]
    cuts = _fork_cuts(parent, set(), new_lines)
    assert cuts.verified is False
    assert cuts.pending is True

  def test_a_file_ending_inside_the_copy_is_pending(self):
    parent = [(0, 'u1'), (1, 'a1')]
    assert _fork_cuts(parent, set(), []).pending is True
    assert _fork_cuts(parent, set(), [json.dumps({'type': 'mode'})]).pending is True
    ancestor_only = [json.dumps({'type': 'user', 'uuid': 'anc1'})]
    assert _fork_cuts(parent, {'anc1'}, ancestor_only).pending is True

  def test_a_record_outside_the_chain_settles_the_decision(self):
    parent = [(0, 'u1'), (1, 'a1')]
    new_lines = [json.dumps({'type': 'user', 'uuid': 'z1'})]
    cuts = _fork_cuts(parent, set(), new_lines)
    assert cuts.verified is False
    assert cuts.pending is False
