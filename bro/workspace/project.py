"""Configuration describing an attached tree."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bro.workspace.paths import find_project_root, project_root


@dataclass(frozen=True)
class ProjectConfig:
  build_context_command: Optional[str] = None
  sections: dict[str, dict[str, Any]] = field(default_factory=dict)


def _bro_table(content: str) -> dict:
  table = tomllib.loads(content).get('tool', {}).get('bro', {})
  if not isinstance(table, dict):
    raise ValueError('[tool.bro] must be a table')
  return table


def _sections(table: dict) -> dict[str, dict[str, Any]]:
  return {key: value for key, value in table.items() if isinstance(value, dict)}


def project_sections_at(root: Optional[Path]) -> dict[str, dict[str, Any]]:
  """The `[tool.bro.<name>]` sub-tables for `root`, or empty when detached."""
  if root is None:
    return {}
  return project_config(root).sections


def project_sections() -> dict[str, dict[str, Any]]:
  return project_sections_at(find_project_root())


def project_config_from_text(content: str, source: str) -> ProjectConfig:
  table = _bro_table(content)
  command = table.get('build-context-command')
  if command is not None and (not isinstance(command, str) or not command):
    raise ValueError(f'[tool.bro] build-context-command in {source} must be a non-empty string')
  return ProjectConfig(build_context_command=command, sections=_sections(table))


def project_config(root: Optional[Path] = None) -> ProjectConfig:
  pyproject = (project_root() if root is None else root) / 'pyproject.toml'
  return (
    project_config_from_text(pyproject.read_text(), str(pyproject))
    if pyproject.is_file()
    else ProjectConfig()
  )
