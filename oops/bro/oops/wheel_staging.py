"""Find workspace distributions that provide a server's declared entries."""

import tomllib
from pathlib import Path

from bro.trails.server.entries import ServedEntry, read_entries


def entry_providers(checkout: Path) -> dict[ServedEntry, str]:
  metadata = tomllib.loads((checkout / 'pyproject.toml').read_text())
  workspace = metadata.get('tool', {}).get('uv', {}).get('workspace', {})
  directories = {checkout}
  for pattern in workspace.get('members', []):
    directories.update(checkout.glob(pattern))
  for pattern in workspace.get('exclude', []):
    directories.difference_update(checkout.glob(pattern))
  providers = {}
  for directory in sorted(directories):
    project = tomllib.loads((directory / 'pyproject.toml').read_text())['project']
    for group, entries in project.get('entry-points', {}).items():
      if group not in {'bro.trail_formats', 'bro.llm.wires'}:
        continue
      for name in entries:
        entry = ServedEntry.model_validate({'group': group, 'name': name})
        if entry in providers:
          raise ValueError(f'duplicate workspace entry {group}:{name}')
        providers[entry] = project['name']
  return providers


def wheel_packages(checkout: Path, declaration: Path) -> tuple[str, ...]:
  providers = entry_providers(checkout)
  packages = {'bro'}
  for entry in read_entries(declaration):
    if entry not in providers:
      raise ValueError(f'no workspace distribution provides {entry.group}:{entry.name}')
    packages.add(providers[entry])
  return tuple(sorted(packages))
