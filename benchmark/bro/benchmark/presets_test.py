"""the committed presets compose into job configs Harbor accepts, and the
committed task sets hold the shape the benchmark README describes."""

import itertools
from collections import defaultdict

import pytest
from harbor.models.job.config import JobConfig

from bro.bench.presets import (
  AGENTS,
  DATASETS,
  PRESETS_DIRECTORY,
  SETTINGS,
  TASKS,
  compose,
  task_set,
)
from bro.benchmark.bundle import workspace_root
from bro.benchmark.job import unknown_fields

FAMILIES = ('category-', 'difficulty-')


def _names(kind: str) -> list[str]:
  names = sorted(path.stem for path in (workspace_root() / PRESETS_DIRECTORY / kind).glob('*.json'))
  assert len(names) > 0, f'no {kind} presets committed'
  return names


def _task_sets() -> dict[str, tuple[str, list[str]]]:
  """name -> (dataset, tasks) of every committed task set."""
  return {name: task_set(workspace_root(), name) for name in _names(TASKS)}


def _family(prefix: str) -> dict[str, tuple[str, list[str]]]:
  """the task sets whose name starts with `prefix`."""
  sets = {name: value for name, value in _task_sets().items() if name.startswith(prefix)}
  assert len(sets) > 0, f'no {prefix} family committed'
  return sets


def _rosters(prefix: str) -> dict[str, set[str]]:
  """dataset -> the union of the family's sets over it."""
  rosters: dict[str, set[str]] = defaultdict(set)
  for dataset, tasks in _family(prefix).values():
    rosters[dataset].update(tasks)
  return dict(rosters)


@pytest.mark.parametrize(
  ('agents', 'settings', 'tasks'),
  list(itertools.product(_names(AGENTS), _names(SETTINGS), _names(TASKS))),
)
def test_every_task_set_composes_with_every_agent_and_settings(agents, settings, tasks):
  config = compose(workspace_root(), agents=agents, settings=settings, tasks=tasks)

  # the presets record rides the config Harbor reads, so Harbor ignoring an
  # unknown key is the contract the composition leans on
  JobConfig.model_validate(config)
  assert unknown_fields(config) == []


@pytest.mark.parametrize('dataset', _names(DATASETS))
def test_every_dataset_pin_composes_whole(dataset):
  config = compose(
    workspace_root(), agents=_names(AGENTS)[0], settings=_names(SETTINGS)[0], dataset=dataset
  )

  assert 'task_names' not in config['datasets'][0]
  JobConfig.model_validate(config)
  assert unknown_fields(config) == []


@pytest.mark.parametrize('family', FAMILIES)
def test_a_family_holds_each_task_of_a_dataset_in_exactly_one_set(family):
  owner: dict[tuple[str, str], str] = {}
  for name, (dataset, tasks) in _family(family).items():
    assert len(tasks) == len(set(tasks)), f'{name} repeats a task'
    for task in tasks:
      assert (dataset, task) not in owner, f'{task} is in both {owner[dataset, task]} and {name}'
      owner[dataset, task] = name


def test_the_families_partition_the_same_rosters():
  category, difficulty = (_rosters(family) for family in FAMILIES)

  assert category == difficulty


@pytest.mark.parametrize('dataset', sorted(_rosters(FAMILIES[0])))
def test_every_selection_over_a_partitioned_dataset_draws_from_its_roster(dataset):
  roster = _rosters(FAMILIES[0])[dataset]
  selections = {
    name: tasks
    for name, (pin, tasks) in _task_sets().items()
    if pin == dataset and not name.startswith(FAMILIES)
  }

  assert len(selections) > 0
  for name, tasks in selections.items():
    assert len(tasks) == len(set(tasks)), f'{name} repeats a task'
    assert set(tasks) <= roster, f'{name} names tasks outside its dataset partition'


def test_smoke_is_within_core():
  smoke_dataset, smoke = task_set(workspace_root(), 'smoke')
  core_dataset, core = task_set(workspace_root(), 'core')

  assert smoke_dataset == core_dataset
  assert set(smoke) <= set(core)
