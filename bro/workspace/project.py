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


def project_sections_at(root: Optional[Path]) -> dict[str, dict[str, Any]]:
  """the `[tool.bro.<name>]` sub-tables for `root`, or empty when detached."""
  if root is None:
    return {}
  pyproject = root / 'pyproject.toml'
  return {} if not pyproject.is_file() else _sections(_bro_table(pyproject))


def project_sections() -> dict[str, dict[str, Any]]:
  """the optional project sub-tables for the repository containing cwd."""
  return project_sections_at(find_project_root())


def project_config(root: Optional[Path] = None) -> ProjectConfig:
  """the validated `[tool.bro]` launch table for `root` (cwd when omitted)."""
  pyproject = (project_root() if root is None else root) / 'pyproject.toml'
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
