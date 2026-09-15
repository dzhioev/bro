import json
from pathlib import Path

import pytest

from bro.bench.presets import (
  AGENTS,
  DATASETS,
  PRESETS_KEY,
  SETTINGS,
  TASKS,
  PresetError,
  Presets,
  compose,
  presets_of,
)

AGENT = {'import_path': 'bro.benchmark.harbor_agent:BroAgent', 'kwargs': {'bro': 'terminal'}}
RUN_SETTINGS = {'n_attempts': 1, 'retry': {'max_retries': 2}}
PIN = {'name': 'org/dataset', 'ref': 'sha256:' + 'd' * 64, 'task_namespace': 'org'}


def _write(tree: Path, kind: str, name: str, value: object) -> Path:
  path = tree / 'benchmark' / kind / f'{name}.json'
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(value))
  return path


@pytest.fixture
def tree(tmp_path):
  _write(tmp_path, AGENTS, 'one', {'agents': [AGENT]})
  _write(tmp_path, SETTINGS, 'quick', RUN_SETTINGS)
  _write(tmp_path, DATASETS, 'pinned', PIN)
  _write(tmp_path, TASKS, 'few', {'dataset': 'pinned', 'tasks': ['alpha', 'beta']})
  _write(tmp_path, TASKS, 'all', {'dataset': 'pinned', 'tasks': []})
  return tmp_path


class TestCompose:
  def test_a_task_set_qualifies_its_names_with_the_pin_namespace(self, tree):
    config = compose(tree, agents='one', settings='quick', tasks='few')

    assert config == {
      PRESETS_KEY: {'agents': 'one', 'settings': 'quick', 'tasks': 'few'},
      **RUN_SETTINGS,
      'agents': [AGENT],
      'datasets': [
        {'name': PIN['name'], 'ref': PIN['ref'], 'task_names': ['org/alpha', 'org/beta']}
      ],
    }

  def test_an_empty_task_set_runs_the_whole_dataset(self, tree):
    config = compose(tree, agents='one', settings='quick', tasks='all')

    assert config['datasets'] == [{'name': PIN['name'], 'ref': PIN['ref']}]

  def test_a_dataset_with_names_is_an_ad_hoc_selection(self, tree):
    config = compose(
      tree, agents='one', settings='quick', dataset='pinned', task_names=['alpha', 'org/beta']
    )

    assert config[PRESETS_KEY]['tasks'] is None
    assert config['datasets'][0]['task_names'] == ['org/alpha', 'org/beta']

  def test_a_dataset_without_names_runs_whole(self, tree):
    config = compose(tree, agents='one', settings='quick', dataset='pinned')

    assert config['datasets'] == [{'name': PIN['name'], 'ref': PIN['ref']}]

  def test_an_empty_ad_hoc_name_is_refused_before_it_reaches_harbor(self, tree):
    with pytest.raises(PresetError, match='non-empty'):
      compose(tree, agents='one', settings='quick', dataset='pinned', task_names=['alpha', ''])

  def test_a_name_outside_the_pin_namespace_is_refused(self, tree):
    with pytest.raises(PresetError, match="outside the dataset namespace 'org'"):
      compose(tree, agents='one', settings='quick', dataset='pinned', task_names=['other/alpha'])

  @pytest.mark.parametrize(
    ('selection', 'reason'),
    [
      ({}, 'a task set or a dataset'),
      ({'tasks': 'few', 'dataset': 'pinned'}, 'not both'),
      ({'tasks': 'few', 'task_names': ['alpha']}, 'carries its own'),
    ],
  )
  def test_the_task_selection_is_one_of_a_set_or_a_dataset(self, tree, selection, reason):
    with pytest.raises(PresetError, match=reason):
      compose(tree, agents='one', settings='quick', **selection)

  def test_a_missing_preset_names_its_kind_and_path(self, tree):
    with pytest.raises(
      PresetError, match=r"no agents preset 'none': .*benchmark/agents/none\.json"
    ):
      compose(tree, agents='none', settings='quick', tasks='few')

  def test_a_preset_name_is_a_file_stem(self, tree):
    with pytest.raises(PresetError, match='file stem'):
      compose(tree, agents='../one', settings='quick', tasks='few')

  @pytest.mark.parametrize(
    ('kind', 'name', 'value', 'reason'),
    [
      (AGENTS, 'one', {'agents': []}, 'non-empty agents list'),
      (AGENTS, 'one', {'agents': [AGENT], 'n_attempts': 1}, 'exactly a non-empty agents list'),
      (SETTINGS, 'quick', {'agents': [AGENT]}, 'carries agents'),
      (SETTINGS, 'quick', {'job_name': 'x', 'jobs_dir': 'y'}, 'carries job_name, jobs_dir'),
      (DATASETS, 'pinned', {'name': PIN['name'], 'ref': PIN['ref']}, 'task_namespace'),
      (DATASETS, 'pinned', {**PIN, 'ref': ''}, 'each a string'),
      (TASKS, 'few', {'dataset': 'pinned', 'tasks': ['org/alpha']}, 'bare task names'),
      (TASKS, 'few', {'dataset': 'pinned'}, 'exactly a dataset'),
      (TASKS, 'few', {'dataset': 'pinned', 'tasks': 'alpha'}, 'bare task names'),
      (SETTINGS, 'quick', [1], 'JSON object'),
    ],
  )
  def test_a_malformed_preset_is_refused(self, tree, kind, name, value, reason):
    _write(tree, kind, name, value)

    with pytest.raises(PresetError, match=reason):
      compose(tree, agents='one', settings='quick', tasks='few')

  def test_invalid_json_names_the_preset(self, tree):
    _write(tree, TASKS, 'few', {}).write_text('{')

    with pytest.raises(PresetError, match="tasks preset 'few' is not valid JSON"):
      compose(tree, agents='one', settings='quick', tasks='few')


class TestPresetsRecord:
  def test_a_composed_config_names_its_presets(self, tree):
    config = compose(tree, agents='one', settings='quick', tasks='few')

    assert presets_of(config) == Presets('one', 'quick', 'few')
    assert presets_of(json.loads(json.dumps(config))).record() == config[PRESETS_KEY]

  def test_a_config_without_a_record_is_refused(self):
    with pytest.raises(PresetError, match="no 'presets' record"):
      presets_of({'agents': [AGENT]})

  @pytest.mark.parametrize(
    'record',
    [
      {'agents': 'one', 'settings': 'quick'},
      {'agents': 'one', 'settings': 'quick', 'tasks': 'few', 'dataset': 'pinned'},
      {'agents': None, 'settings': 'quick', 'tasks': None},
      {'agents': 'one', 'settings': 'quick', 'tasks': 'a/b'},
      'one',
    ],
  )
  def test_a_malformed_record_is_refused(self, record):
    with pytest.raises(PresetError):
      Presets.from_record(record)
