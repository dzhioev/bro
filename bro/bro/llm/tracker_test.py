import json
from pathlib import Path

import pytest

import configs
from llm.tracker import LocalFileTracker, NullTracker, Parent, Tracker


def _read_jsonl(path: Path) -> list[dict]:
  return [json.loads(line) for line in path.read_text().splitlines() if len(line) > 0]


class TestNullTracker:
  def test_start_trail_returns_empty_string(self):
    t = NullTracker()
    trail_id = t.start_trail(
      bro='b', llm_spec={}, system_prompt='', parent=None, interactive=False, entry_point='x'
    )
    assert trail_id == ''

  def test_methods_are_noops(self):
    t = NullTracker()
    t.start_trail(
      bro='b', llm_spec={}, system_prompt='p', parent=None, interactive=True, entry_point='x'
    )
    t.step('reasoning', 'r', turn_index=1)
    t.step('end', {'reason': 'terminal'})
    t.end_trail('terminal')


class TestLocalFileTrackerStartTrail:
  def test_writes_header_line_with_metadata(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    trail_id = tracker.start_trail(
      bro='echo',
      llm_spec={'type': 'echo', 'model': 'm'},
      system_prompt='do the thing',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.close()
    records = _read_jsonl(path)
    # header line + the auto-emitted system_prompt step
    assert len(records) == 2
    header = records[0]
    assert header['record_type'] == 'trail'
    assert header['trail_id'] == trail_id
    assert header['bro'] == 'echo'
    assert header['bro_version'] == configs.VERSION
    assert header['llm_spec'] == {'type': 'echo', 'model': 'm'}
    assert header['interactive'] is False
    assert header['entry_point'] == 'cli:bro_run'
    assert header['parent'] is None
    assert 'started_at' in header

  def test_auto_emits_system_prompt_as_first_step(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    trail_id = tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='full prompt text',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.close()
    records = _read_jsonl(path)
    step = records[1]
    assert step['record_type'] == 'step'
    assert step['trail_id'] == trail_id
    assert step['kind'] == 'system_prompt'
    assert step['body'] == 'full prompt text'
    assert step['turn_index'] == 0
    assert 'step_id' in step
    assert 'ts' in step

  def test_parent_is_serialized_when_present(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    parent = Parent(trail_id='abc', step_id='def', relationship='fork')
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='',
      parent=parent,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.close()
    header = _read_jsonl(path)[0]
    assert header['parent'] == {'trail_id': 'abc', 'step_id': 'def', 'relationship': 'fork'}

  def test_bro_version_comes_from_configs(self, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(configs, 'VERSION', 42)
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.close()
    assert _read_jsonl(path)[0]['bro_version'] == 42


class TestLocalFileTrackerStep:
  def test_appends_step_with_extras(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.step(
      'tool_call',
      None,
      tool_name='add_task',
      arguments={'name': 'x'},
      call_id='c1',
      turn_index=1,
    )
    tracker.close()
    records = _read_jsonl(path)
    # header + system_prompt step + tool_call step
    assert len(records) == 3
    step = records[2]
    assert step['kind'] == 'tool_call'
    assert step['body'] is None
    assert step['tool_name'] == 'add_task'
    assert step['arguments'] == {'name': 'x'}
    assert step['call_id'] == 'c1'
    assert step['turn_index'] == 1

  def test_step_before_start_trail_raises(self, tmp_path: Path):
    tracker = LocalFileTracker(tmp_path / 'trail.jsonl')
    with pytest.raises(RuntimeError):
      tracker.step('reasoning', 'thinking')

  def test_each_step_gets_unique_step_id(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.step('reasoning', 'a')
    tracker.step('reasoning', 'b')
    tracker.close()
    records = _read_jsonl(path)
    step_ids = [r['step_id'] for r in records if r['record_type'] == 'step']
    assert len(set(step_ids)) == len(step_ids)


class TestLocalFileTrackerEndTrail:
  def test_emits_end_step(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.end_trail('terminal')
    tracker.close()
    records = _read_jsonl(path)
    end = records[-1]
    assert end['record_type'] == 'step'
    assert end['kind'] == 'end'
    assert end['body'] == {'reason': 'terminal'}

  def test_second_end_trail_is_noop(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.end_trail('terminal')
    tracker.end_trail('raised')
    tracker.close()
    end_records = [r for r in _read_jsonl(path) if r.get('kind') == 'end']
    assert len(end_records) == 1


class TestLocalFileTrackerAppend:
  def test_multiple_trails_coexist_in_one_file(self, tmp_path: Path):
    path = tmp_path / 'trail.jsonl'
    tracker = LocalFileTracker(path)
    first = tracker.start_trail(
      bro='a',
      llm_spec={},
      system_prompt='p1',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.end_trail('terminal')
    second = tracker.start_trail(
      bro='b',
      llm_spec={},
      system_prompt='p2',
      parent=None,
      interactive=False,
      entry_point='cli:bro_run',
    )
    tracker.end_trail('terminal')
    tracker.close()
    records = _read_jsonl(path)
    trail_ids = {r['trail_id'] for r in records}
    assert trail_ids == {first, second}
    assert first != second


class TestTrackerIsABC:
  def test_cannot_instantiate_base_class(self):
    with pytest.raises(TypeError):
      Tracker()  # type: ignore[abstract]
