import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.workspace.paths import project_root


def _default_image_repository(default_bro: str) -> str:
  return f'bro/{default_bro}'


@dataclass(frozen=True)
class ProjectConfig:
  """the operated repo's launch defaults: which bro a session runs as when
  `--bro` doesn't name one, the docker repository its session images build
  under (`bro/<default bro>` unless overridden), and the optional
  build-context-file-list command."""

  default_bro: str
  image_repository: str
  build_context_command: Optional[str] = None


def _parse_command(table: dict, pyproject: Path, key: str) -> Optional[str]:
  command = table.get(key)
  if command is not None and (not isinstance(command, str) or command == ''):
    raise ValueError(f'[tool.bro] {key} in {pyproject} must be a non-empty string')
  return command


def project_config() -> ProjectConfig:
  """the `[tool.bro]` table of the operated repo's pyproject.toml, read from
  `project_root()` — how a repo declares its session defaults. a missing file,
  required default, or unknown key raises rather than being ignored."""
  pyproject = project_root() / 'pyproject.toml'
  if not pyproject.is_file():
    raise ValueError(f'missing {pyproject}')
  table = tomllib.loads(pyproject.read_text()).get('tool', {}).get('bro', {})
  unknown = sorted(set(table) - {'default', 'image-repository', 'build-context-command'})
  if len(unknown) > 0:
    raise ValueError(f'unknown [tool.bro] key(s) in {pyproject}: {", ".join(unknown)}')
  default_bro = table.get('default')
  if default_bro is None:
    raise ValueError(f'missing [tool.bro] default in {pyproject}')
  if not isinstance(default_bro, str):
    raise ValueError(f'[tool.bro] default in {pyproject} must be a string')
  override: Optional[str] = table.get('image-repository')
  return ProjectConfig(
    default_bro=default_bro,
    image_repository=override if override is not None else _default_image_repository(default_bro),
    build_context_command=_parse_command(table, pyproject, 'build-context-command'),
  )
