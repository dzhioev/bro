import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bro.workspace.paths import find_project_root, project_root

_LAUNCH_KEYS = frozenset({'default', 'harness', 'image-repository', 'build-context-command'})


def _default_image_repository(default_bro: str) -> str:
  return f'bro/{default_bro}'


@dataclass(frozen=True)
class ProjectConfig:
  """the operated repo's launch defaults: which bro a session runs as when
  `--bro` doesn't name one, the docker repository its session images build
  under (`bro/<default bro>` unless overridden), and the optional
  build-context-file-list command.

  `sections` carries the `[tool.bro.<name>]` sub-tables verbatim. Their keys
  belong to whoever declares them, so they are read but never interpreted here.
  """

  default_bro: str
  image_repository: str
  harness: str = 'claude'
  build_context_command: Optional[str] = None
  sections: dict[str, dict[str, Any]] = field(default_factory=dict)


def _optional_string(table: dict, pyproject: Path, key: str) -> Optional[str]:
  value = table.get(key)
  if value is not None and (not isinstance(value, str) or value == ''):
    raise ValueError(f'[tool.bro] {key} in {pyproject} must be a non-empty string')
  return value


def _bro_table(pyproject: Path) -> dict:
  return tomllib.loads(pyproject.read_text()).get('tool', {}).get('bro', {})


def _sections(table: dict) -> dict[str, dict[str, Any]]:
  return {key: value for key, value in table.items() if isinstance(value, dict)}


def project_sections() -> dict[str, dict[str, Any]]:
  """the `[tool.bro.<name>]` sub-tables of the operated repo's pyproject.toml,
  empty where there is no repo to read one from.

  A sub-table is configuration a reader can do without, so this asks only for
  what it needs and tolerates the absence of a project — unlike `project_config`,
  whose launch keys a managed repo must declare. Callers run in whatever
  directory they were started in, which is not always inside a checkout.
  """
  root = find_project_root()
  if root is None:
    return {}
  pyproject = root / 'pyproject.toml'
  return {} if not pyproject.is_file() else _sections(_bro_table(pyproject))


def project_config() -> ProjectConfig:
  """the `[tool.bro]` table of the operated repo's pyproject.toml, read from
  `project_root()` — how a repo declares its session defaults. a missing file,
  required default, or unknown key raises rather than being ignored."""
  pyproject = project_root() / 'pyproject.toml'
  if not pyproject.is_file():
    raise ValueError(f'missing {pyproject}')
  table = _bro_table(pyproject)
  sections = _sections(table)
  unknown = sorted(set(table) - _LAUNCH_KEYS - set(sections))
  if len(unknown) > 0:
    raise ValueError(f'unknown [tool.bro] key(s) in {pyproject}: {", ".join(unknown)}')
  default_bro = table.get('default')
  if default_bro is None:
    raise ValueError(f'missing [tool.bro] default in {pyproject}')
  if not isinstance(default_bro, str):
    raise ValueError(f'[tool.bro] default in {pyproject} must be a string')
  override: Optional[str] = table.get('image-repository')
  harness = _optional_string(table, pyproject, 'harness') or 'claude'
  if harness not in ('claude', 'bro'):
    raise ValueError(f'[tool.bro] harness in {pyproject} must be `claude` or `bro`')
  return ProjectConfig(
    default_bro=default_bro,
    image_repository=override if override is not None else _default_image_repository(default_bro),
    harness=harness,
    build_context_command=_optional_string(table, pyproject, 'build-context-command'),
    sections=sections,
  )
