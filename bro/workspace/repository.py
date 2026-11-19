"""Repository-owned configuration."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RepositoryConfig:
  provision_command: str | None = None
  sections: dict[str, dict[str, Any]] = field(default_factory=dict)


def repository_config(root: Path) -> RepositoryConfig:
  path = root / '.bro' / 'config.toml'
  try:
    content = path.read_text()
  except FileNotFoundError:
    return RepositoryConfig()
  try:
    table = tomllib.loads(content)
  except tomllib.TOMLDecodeError as error:
    raise ValueError(f'{path}: {error}') from error
  command = None
  sections = {}
  for key, value in table.items():
    if key == 'provision-command':
      if not isinstance(value, str) or not value:
        raise ValueError(f'{path}: {key} must be a non-empty string')
      command = value
    elif isinstance(value, dict):
      sections[key] = value
    else:
      raise ValueError(f'{path}: unknown top-level key {key!r}')
  return RepositoryConfig(provision_command=command, sections=sections)
