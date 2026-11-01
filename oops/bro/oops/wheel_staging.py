"""Resolve an image's framework declaration to the workspace distributions it stages."""

import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name
from pydantic import BaseModel, ConfigDict, TypeAdapter


class DeclaredEntry(BaseModel, frozen=True):
  model_config = ConfigDict(extra='forbid')

  group: str
  name: str


class DeclaredDistribution(BaseModel, frozen=True):
  model_config = ConfigDict(extra='forbid')

  distribution: str


def read_declaration(path: Path) -> tuple[DeclaredEntry | DeclaredDistribution, ...]:
  return TypeAdapter(tuple[DeclaredEntry | DeclaredDistribution, ...]).validate_json(
    path.read_text()
  )


def workspace_projects(checkout: Path) -> dict[NormalizedName, dict[str, Any]]:
  metadata = tomllib.loads((checkout / 'pyproject.toml').read_text())
  workspace = metadata.get('tool', {}).get('uv', {}).get('workspace', {})
  directories = {checkout}
  for pattern in workspace.get('members', []):
    directories.update(checkout.glob(pattern))
  for pattern in workspace.get('exclude', []):
    directories.difference_update(checkout.glob(pattern))
  projects = {}
  for directory in sorted(directories):
    project = tomllib.loads((directory / 'pyproject.toml').read_text())['project']
    projects[canonicalize_name(project['name'])] = project
  return projects


def entry_providers(
  projects: dict[NormalizedName, dict[str, Any]],
) -> dict[DeclaredEntry, NormalizedName]:
  providers = {}
  for distribution, project in projects.items():
    for group, entries in project.get('entry-points', {}).items():
      for name in entries:
        entry = DeclaredEntry(group=group, name=name)
        if entry in providers:
          raise ValueError(f'duplicate workspace entry {group}:{name}')
        providers[entry] = distribution
  return providers


def wheel_packages(checkout: Path, declaration: Path | None) -> tuple[str, ...]:
  projects = workspace_projects(checkout)
  providers = entry_providers(projects)
  pending = [canonicalize_name('bro')]
  for item in () if declaration is None else read_declaration(declaration):
    if isinstance(item, DeclaredDistribution):
      distribution = canonicalize_name(item.distribution)
      if distribution not in projects:
        raise ValueError(f'{item.distribution} is not a workspace distribution')
      pending.append(distribution)
    elif item in providers:
      pending.append(providers[item])
    else:
      raise ValueError(f'no workspace distribution provides {item.group}:{item.name}')
  staged = set()
  while pending:
    distribution = pending.pop()
    if distribution in staged:
      continue
    staged.add(distribution)
    for requirement in projects[distribution].get('dependencies', []):
      dependency = canonicalize_name(Requirement(requirement).name)
      if dependency in projects:
        pending.append(dependency)
  return tuple(sorted(projects[distribution]['name'] for distribution in staged))
