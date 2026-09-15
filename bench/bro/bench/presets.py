"""Compose a Harbor job config from the presets committed under `benchmark/`,
a JSON file per preset under its kind's directory; the composed config carries
the names it was composed from under `presets`, a key Harbor ignores.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

PRESETS_DIRECTORY = Path('benchmark')
AGENTS = 'agents'
SETTINGS = 'settings'
DATASETS = 'datasets'
TASKS = 'tasks'
PRESETS_KEY = 'presets'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
# Harbor fields no run settings may carry: each is another preset's, or the
# job runner's own
_RESERVED_SETTINGS = frozenset({'agents', 'datasets', 'tasks', 'job_name', 'jobs_dir', PRESETS_KEY})


class PresetError(ValueError):
  """a preset that is missing or malformed, or a selection that does not compose."""


@dataclass(frozen=True)
class Presets:
  """the names a run is composed from; `tasks` is None for an ad hoc task selection."""

  agents: str
  settings: str
  tasks: Optional[str]

  def record(self) -> dict[str, Optional[str]]:
    return {'agents': self.agents, 'settings': self.settings, 'tasks': self.tasks}

  @classmethod
  def from_record(cls, value: Any) -> 'Presets':
    if not isinstance(value, dict) or set(value) != {'agents', 'settings', 'tasks'}:
      raise PresetError('a presets record carries exactly agents, settings, and tasks')
    agents = _preset_name(value['agents'], AGENTS)
    settings = _preset_name(value['settings'], SETTINGS)
    tasks = None if value['tasks'] is None else _preset_name(value['tasks'], TASKS)
    return cls(agents, settings, tasks)


def _preset_name(value: Any, kind: str) -> str:
  if not isinstance(value, str) or _NAME.fullmatch(value) is None:
    raise PresetError(f'a {kind} preset name is a file stem, got {value!r}')
  return value


def preset_path(tree: Path, kind: str, name: str) -> Path:
  return tree / PRESETS_DIRECTORY / kind / f'{_preset_name(name, kind)}.json'


def _load(tree: Path, kind: str, name: str) -> dict[str, Any]:
  path = preset_path(tree, kind, name)
  if not path.is_file():
    raise PresetError(f'no {kind} preset {name!r}: {path}')
  try:
    value = json.loads(path.read_text())
  except json.JSONDecodeError as error:
    raise PresetError(f'{kind} preset {name!r} is not valid JSON: {error}') from error
  if not isinstance(value, dict):
    raise PresetError(f'{kind} preset {name!r} must be a JSON object')
  return value


def _string(value: Any) -> bool:
  return isinstance(value, str) and len(value) > 0


def _agents(tree: Path, name: str) -> list[Any]:
  preset = _load(tree, AGENTS, name)
  roster = preset.get('agents')
  if set(preset) != {'agents'} or not isinstance(roster, list) or len(roster) == 0:
    raise PresetError(f'{AGENTS} preset {name!r} carries exactly a non-empty agents list')
  return roster


def _settings(tree: Path, name: str) -> dict[str, Any]:
  preset = _load(tree, SETTINGS, name)
  reserved = sorted(_RESERVED_SETTINGS & set(preset))
  if len(reserved) > 0:
    raise PresetError(f'{SETTINGS} preset {name!r} carries {", ".join(reserved)}')
  return preset


def _dataset(tree: Path, name: str) -> dict[str, str]:
  preset = _load(tree, DATASETS, name)
  if set(preset) != {'name', 'ref', 'task_namespace'} or not all(map(_string, preset.values())):
    raise PresetError(
      f'{DATASETS} preset {name!r} carries exactly name, ref, and task_namespace, each a string'
    )
  return preset


def _task_set(tree: Path, name: str) -> tuple[str, list[str]]:
  preset = _load(tree, TASKS, name)
  tasks = preset.get('tasks')
  if (
    set(preset) != {'dataset', 'tasks'}
    or not isinstance(tasks, list)
    or not all(_string(task) and '/' not in task for task in tasks)
  ):
    raise PresetError(f'{TASKS} preset {name!r} carries exactly a dataset and bare task names')
  return _preset_name(preset['dataset'], DATASETS), tasks


def _qualified(task: str, namespace: str) -> str:
  """the task's name inside the dataset, the way Harbor matches it."""
  prefix, slash, _ = task.partition('/')
  if slash == '':
    return f'{namespace}/{task}'
  if prefix != namespace:
    raise PresetError(f'task {task!r} is outside the dataset namespace {namespace!r}')
  return task


def compose(
  tree: Path,
  *,
  agents: str,
  settings: str,
  tasks: Optional[str] = None,
  dataset: Optional[str] = None,
  task_names: Sequence[str] = (),
) -> dict[str, Any]:
  """the Harbor job config the named presets compose into, carrying their names
  under `presets`. A task set names its dataset and tasks; a dataset pin with
  `task_names` (bare or Harbor-qualified) is an ad hoc selection, and with none
  the whole dataset."""
  if tasks is None:
    if dataset is None:
      raise PresetError('a run names a task set or a dataset')
    selected = list(task_names)
    if not all(map(_string, selected)):
      raise PresetError('a task name is a non-empty string')
  else:
    if dataset is not None:
      raise PresetError('a run names a task set or a dataset, not both')
    if len(task_names) > 0:
      raise PresetError('task names go with a dataset; a task set carries its own')
    dataset, selected = _task_set(tree, tasks)
  roster = _agents(tree, agents)
  run_settings = _settings(tree, settings)
  pin = _dataset(tree, dataset)
  selection: dict[str, Any] = {'name': pin['name'], 'ref': pin['ref']}
  if len(selected) > 0:
    selection['task_names'] = [_qualified(task, pin['task_namespace']) for task in selected]
  return {
    PRESETS_KEY: Presets(agents, settings, tasks).record(),
    **run_settings,
    'agents': roster,
    'datasets': [selection],
  }


def presets_of(config: Any) -> Presets:
  """the presets a composed job config names."""
  if not isinstance(config, dict) or PRESETS_KEY not in config:
    raise PresetError(f'the job config carries no {PRESETS_KEY!r} record')
  return Presets.from_record(config[PRESETS_KEY])
