"""Plug-in imports and provider literals require a direct distribution dependency."""

import ast
import re
import subprocess
import tomllib
from pathlib import Path

from bro.dev.packaging_policy import distribution_roots

_ROOT = Path(__file__).resolve().parents[3]
_GROUPS = (
  'bro.llm.providers',
  'bro.llm.wires',
  'bro.llm.wire_clients',
  'bro.harnesses',
  'bro.trail_formats',
)


def _dependencies(project: dict) -> set[str]:
  return {
    re.split(r'[\[<>=!~; ]', value)[0].lower().replace('_', '-')
    for value in project.get('dependencies', [])
  }


def _violations(root: Path) -> list[str]:
  directories = distribution_roots(root)
  metadata = {
    directory: tomllib.loads((directory / 'pyproject.toml').read_text())
    for directory in directories
  }
  packages: dict[str, str] = {}
  providers: dict[str, str] = {}
  for data in metadata.values():
    project = data['project']
    owner = project['name']
    entries = project.get('entry-points', {})
    for group in _GROUPS:
      for name, target in entries.get(group, {}).items():
        module = target.split(':')[0]
        # A declaration and its credential module share the provider package.
        package = module.rpartition('.')[0]
        packages[package] = owner
        if group == 'bro.llm.providers':
          providers[name] = owner
  listing = subprocess.run(
    ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '--', '*.py'],
    cwd=root,
    capture_output=True,
    text=True,
    check=True,
  ).stdout
  result = []
  for relative in listing.splitlines():
    path = root / relative
    if (
      not path.is_file()
      or path.name.endswith(('_test.py', '_test_helper.py'))
      or path.name == 'conftest.py'
    ):
      continue
    directory = next(
      directory
      for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True)
      if path.is_relative_to(directory)
    )
    project = metadata[directory]['project']
    allowed = _dependencies(project) | {project['name']}
    for node in ast.walk(ast.parse(path.read_text())):
      imported = []
      if isinstance(node, ast.Import):
        imported = [alias.name for alias in node.names]
      elif isinstance(node, ast.ImportFrom):
        if node.level:
          continue
        imported = [node.module or '', *(f'{node.module}.{alias.name}' for alias in node.names)]
      for module in imported:
        for package, owner in packages.items():
          if (module == package or module.startswith(package + '.')) and owner not in allowed:
            result.append(f'{relative}:{node.lineno}: import {module} requires {owner}')
      if isinstance(node, ast.Constant) and isinstance(node.value, str):
        owner = providers.get(node.value)
        if (
          owner is not None
          and owner not in allowed
          and not (relative == 'bro/brash.py' and node.value == 'echo')
        ):
          result.append(f'{relative}:{node.lineno}: provider {node.value!r} requires {owner}')
  return sorted(set(result))


def test_workspace_respects_plugin_boundaries():
  assert _violations(_ROOT) == []


def test_policy_checks_imports_literals_and_dependencies(tmp_path):
  subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
  (tmp_path / 'pyproject.toml').write_text("""
[project]
name = "core"
[tool.uv.workspace]
members = ["plugin", "consumer"]
""")
  (tmp_path / 'plugin').mkdir()
  (tmp_path / 'plugin/pyproject.toml').write_text("""
[project]
name = "plugin"
[project.entry-points."bro.llm.providers"]
fixture = "bro.fixture.declaration"
""")
  (tmp_path / 'consumer').mkdir()
  (tmp_path / 'consumer/pyproject.toml').write_text("""
[project]
name = "consumer"
dependencies = ["plugin[client]>=1"]
""")
  source = "from bro.fixture import declaration\nimport bro.fixture.credentials\nname = 'fixture'\n"
  (tmp_path / 'outside.py').write_text(source)
  (tmp_path / 'consumer/inside.py').write_text(source)
  (tmp_path / 'ignored_test.py').write_text(source)
  assert _violations(tmp_path) == [
    'outside.py:1: import bro.fixture requires plugin',
    'outside.py:1: import bro.fixture.declaration requires plugin',
    'outside.py:2: import bro.fixture.credentials requires plugin',
    "outside.py:3: provider 'fixture' requires plugin",
  ]
