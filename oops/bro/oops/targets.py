import contextlib
import importlib
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.workspace.paths import find_project_root
from bro.workspace.project import project_sections_at

_TARGET_NAME = re.compile(r'[a-z0-9][a-z0-9-]*')
_REGISTRY_KEY = 'target-registry'

# A plan command exits with this status to report a change it judges unsafe to deploy; any
# other non-zero status means the plan did not complete, so nothing was checked.
PLAN_UNSAFE_EXIT_CODE = 3


def _non_empty(value: str, field: str) -> None:
  if not isinstance(value, str) or value == '':
    raise ValueError(f'{field} must be a non-empty string')


@dataclass(frozen=True)
class Command:
  path: str
  arguments: tuple[str, ...] = ()

  def __post_init__(self) -> None:
    _non_empty(self.path, 'command path')
    if Path(self.path).is_absolute():
      raise ValueError(f'command path must be relative to the repository: {self.path!r}')
    if any(not isinstance(argument, str) or argument == '' for argument in self.arguments):
      raise ValueError('command arguments must be non-empty strings')


@dataclass(frozen=True)
class ECSService:
  region: str
  cluster: str
  service: str

  def __post_init__(self) -> None:
    _non_empty(self.region, 'ECS region')
    _non_empty(self.cluster, 'ECS cluster')
    _non_empty(self.service, 'ECS service')


@dataclass(frozen=True)
class HeaderAuth:
  header: str
  value: str

  def __post_init__(self) -> None:
    _non_empty(self.header, 'probe auth header')
    _non_empty(self.value, 'probe auth value')


@dataclass(frozen=True)
class SSMParameterAuth:
  header: str
  prefix: str
  parameter: str
  region: str

  def __post_init__(self) -> None:
    _non_empty(self.header, 'probe auth header')
    _non_empty(self.parameter, 'probe auth parameter')
    _non_empty(self.region, 'probe auth region')


ProbeAuth = HeaderAuth | SSMParameterAuth


@dataclass(frozen=True)
class HTTPProbe:
  url: str
  auth: Optional[ProbeAuth] = None

  def __post_init__(self) -> None:
    _non_empty(self.url, 'probe URL')


@dataclass(frozen=True)
class DeployTarget:
  deploy: Command
  plan: Optional[Command] = None
  verify: Optional[Command] = None
  ecs: Optional[ECSService] = None
  probe: Optional[HTTPProbe] = None
  paths: tuple[str, ...] = ()
  notes: str = ''

  def __post_init__(self) -> None:
    if any(not isinstance(path, str) or path == '' for path in self.paths):
      raise ValueError('target paths must be non-empty strings')


@dataclass(frozen=True)
class TargetRegistry:
  load_targets: Callable[[], Mapping[str, DeployTarget]]
  needed_secrets: tuple[str, ...]

  def __post_init__(self) -> None:
    if any(not isinstance(name, str) or name == '' for name in self.needed_secrets):
      raise ValueError('registry credential names must be non-empty strings')
    if len(set(self.needed_secrets)) != len(self.needed_secrets):
      raise ValueError('registry credential names must be unique')

  def targets(self) -> dict[str, DeployTarget]:
    targets = dict(self.load_targets())
    if len(targets) == 0:
      raise ValueError('a deploy-target registry must contain at least one target')
    for name, target in targets.items():
      if _TARGET_NAME.fullmatch(name) is None:
        raise ValueError(f'invalid deploy target name: {name!r}')
      if not isinstance(target, DeployTarget):
        raise TypeError(f'deploy target {name!r} must be a DeployTarget')
    return targets


@contextlib.contextmanager
def _project_import_path(root: Path) -> Iterator[None]:
  original = sys.path[:]
  sys.path.insert(0, str(root))
  try:
    yield
  finally:
    sys.path[:] = original


def load_project_registry(root: Optional[Path] = None) -> Optional[TargetRegistry]:
  project_root = find_project_root() if root is None else root
  if project_root is None:
    return None
  section = project_sections_at(project_root).get('devoops')
  if section is None:
    return None
  unknown = set(section) - {_REGISTRY_KEY}
  if len(unknown) > 0:
    raise ValueError(f'[tool.bro.devoops] has unknown fields: {", ".join(sorted(unknown))}')
  reference = section.get(_REGISTRY_KEY)
  if reference is None:
    return None
  if not isinstance(reference, str) or reference.count(':') != 1:
    raise ValueError(f'[tool.bro.devoops] {_REGISTRY_KEY} must be a module:attribute reference')
  module_name, attribute_name = reference.split(':')
  _non_empty(module_name, f'[tool.bro.devoops] {_REGISTRY_KEY} module')
  _non_empty(attribute_name, f'[tool.bro.devoops] {_REGISTRY_KEY} attribute')
  with _project_import_path(project_root):
    registry = getattr(importlib.import_module(module_name), attribute_name)
  if not isinstance(registry, TargetRegistry):
    raise TypeError(f'{reference} must resolve to a TargetRegistry')
  return registry
