import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bro.base import credentials
from bro.workspace.paths import project_root


def _default_image_repository(default_bro: str) -> str:
  return f'bro/{default_bro}'


@dataclass(frozen=True)
class ProjectConfig:
  """the operated repo's launch defaults: which bro a session runs as when
  `--bro` doesn't name one, the docker repository its session images build
  under (`bro/<default bro>` unless overridden), the per-kind credential
  instances its launches substitute in computed scopes, and the optional
  squash-footer command."""

  default_bro: str
  image_repository: str
  creds: dict[str, str] = field(default_factory=dict)
  footer_command: Optional[str] = None


def _parse_creds(table: dict, pyproject: Path) -> dict[str, str]:
  creds = table.get('creds', {})
  if not isinstance(creds, dict):
    raise ValueError(f'[tool.bro] creds in {pyproject} must be a table of kind = "instance"')
  for kind, instance in creds.items():
    if not isinstance(instance, str):
      raise ValueError(
        f'[tool.bro] creds entry {kind!r} in {pyproject}: instance must be a string, '
        f'got {instance!r}'
      )
    try:
      credentials.parse_name(f'{kind}+{instance}')
    except ValueError as e:
      raise ValueError(f'[tool.bro] creds entry {kind!r} = {instance!r} in {pyproject}: {e}') from e
  return creds


def project_config() -> ProjectConfig:
  """the `[tool.bro]` table of the operated repo's pyproject.toml, read from
  `project_root()` — how a repo declares its session defaults. a missing file,
  required default, or unknown key raises rather than being ignored."""
  pyproject = project_root() / 'pyproject.toml'
  if not pyproject.is_file():
    raise ValueError(f'missing {pyproject}')
  table = tomllib.loads(pyproject.read_text()).get('tool', {}).get('bro', {})
  unknown = sorted(set(table) - {'default', 'image-repository', 'creds', 'footer-command'})
  if len(unknown) > 0:
    raise ValueError(f'unknown [tool.bro] key(s) in {pyproject}: {", ".join(unknown)}')
  default_bro = table.get('default')
  if default_bro is None:
    raise ValueError(f'missing [tool.bro] default in {pyproject}')
  if not isinstance(default_bro, str):
    raise ValueError(f'[tool.bro] default in {pyproject} must be a string')
  override: Optional[str] = table.get('image-repository')
  footer_command = table.get('footer-command')
  if footer_command is not None and (not isinstance(footer_command, str) or footer_command == ''):
    raise ValueError(f'[tool.bro] footer-command in {pyproject} must be a non-empty string')
  return ProjectConfig(
    default_bro=default_bro,
    image_repository=override if override is not None else _default_image_repository(default_bro),
    creds=_parse_creds(table, pyproject),
    footer_command=footer_command,
  )
