import tomllib
from dataclasses import dataclass, field
from typing import Optional

from workspace.paths import project_root

# the `--persona` default — the bro a session runs as when neither --persona
# nor --bro names one.
DEFAULT_SESSION_BRO = 'ppp-dev'


def _default_image_repository(persona: str) -> str:
  return f'bro/{persona}'


@dataclass(frozen=True)
class ProjectConfig:
  """the operated repo's launch defaults: which bro a session runs as when
  neither --persona nor --bro names one, and the docker repository its session
  images build under (`bro/<persona>` unless overridden — the derivation lives
  in project_config)."""

  persona: str = DEFAULT_SESSION_BRO
  image_repository: str = field(default=_default_image_repository(DEFAULT_SESSION_BRO))


def project_config() -> ProjectConfig:
  """the `[tool.bro]` table of the operated repo's pyproject.toml, read from
  `project_root()` — how a repo that carries another project than ppp declares
  its own session defaults. an absent file or table means the ppp defaults; an
  unknown key raises rather than being ignored."""
  pyproject = project_root() / 'pyproject.toml'
  if not pyproject.is_file():
    return ProjectConfig()
  table = tomllib.loads(pyproject.read_text()).get('tool', {}).get('bro', {})
  unknown = sorted(set(table) - {'persona', 'image-repository'})
  if len(unknown) > 0:
    raise ValueError(f'unknown [tool.bro] key(s) in {pyproject}: {", ".join(unknown)}')
  persona = table.get('persona', DEFAULT_SESSION_BRO)
  override: Optional[str] = table.get('image-repository')
  return ProjectConfig(
    persona=persona,
    image_repository=override if override is not None else _default_image_repository(persona),
  )
