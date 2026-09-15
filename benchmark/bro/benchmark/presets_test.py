"""the committed presets compose into job configs Harbor accepts."""

import itertools

import pytest
from harbor.models.job.config import JobConfig

from bro.bench.presets import AGENTS, DATASETS, PRESETS_DIRECTORY, SETTINGS, TASKS, compose
from bro.benchmark.bundle import workspace_root
from bro.benchmark.job import unknown_fields


def _names(kind: str) -> list[str]:
  names = sorted(path.stem for path in (workspace_root() / PRESETS_DIRECTORY / kind).glob('*.json'))
  assert len(names) > 0, f'no {kind} presets committed'
  return names


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
