"""Plug-in imports and provider literals require a direct distribution dependency."""

import ast
import importlib.util
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from bro.dev.packaging_policy import distribution_roots
from bro.local.run_tests import PROJECTS

_ROOT = Path(__file__).resolve().parents[3]
_PROVIDER_WIRE_GROUPS = (
  'bro.llm.providers',
  'bro.llm.wires',
  'bro.llm.wire_clients',
)
_GROUPS = (
  *_PROVIDER_WIRE_GROUPS,
  'bro.harnesses',
  'bro.trail_formats',
)

_SDK_PACKAGES = {'openai', 'anthropic'}


def _dependency_name(value: str) -> str:
  return re.split(r'[\[<>=!~; ]', value)[0].lower().replace('_', '-')


def _dependencies(project: dict) -> set[str]:
  return {_dependency_name(value) for value in project.get('dependencies', [])}


def _violations(root: Path) -> list[str]:
  directories = distribution_roots(root, [project.directory for project in PROJECTS])
  metadata = {
    directory: tomllib.loads((directory / 'pyproject.toml').read_text())
    for directory in directories
  }
  packages: dict[str, str] = {}
  providers: dict[str, str] = {}
  for directory, data in metadata.items():
    project = data['project']
    owner = project['name']
    entries = project.get('entry-points', {})
    if any(entries.get(group) for group in _GROUPS):
      module_root = directory / data.get('tool', {}).get('uv', {}).get('build-backend', {}).get(
        'module-root', ''
      )
      for source in module_root.rglob('*.py'):
        if source.name.endswith(('_test.py', '_test_helper.py')) or source.name == 'conftest.py':
          continue
        parts = source.relative_to(module_root).with_suffix('').parts
        module = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
        packages[module] = owner
    for group in _GROUPS:
      for name, target in entries.get(group, {}).items():
        module = target.split(':')[0]
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
      if isinstance(node, (ast.Import, ast.ImportFrom)):
        if isinstance(node, ast.Import):
          imported = [alias.name for alias in node.names]
        else:
          module = node.module or ''
          if node.level:
            parts = path.relative_to(directory).with_suffix('').parts
            package = '.'.join(parts[:-1])
            module = importlib.util.resolve_name('.' * node.level + module, package)
          imported = [module, *(f'{module}.{alias.name}' for alias in node.names)]
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


def _sdk_violations(project: dict) -> list[str]:
  entries = project.get('entry-points', {})
  if not any(entries.get(group) for group in _PROVIDER_WIRE_GROUPS):
    return []
  result = []
  dependency_sets = [
    ('required', project.get('dependencies', [])),
    *project.get('optional-dependencies', {}).items(),
  ]
  for group, dependencies in dependency_sets:
    for dependency in dependencies:
      package = _dependency_name(dependency)
      extras = {
        extra.strip() for extra in dependency.partition('[')[2].partition(']')[0].split(',')
      }
      if group != 'client' and (package in _SDK_PACKAGES or 'client' in extras):
        result.append(f'{project["name"]}: {group} dependency {dependency} must be in client extra')
  return result


def test_provider_and_wire_sdks_are_only_client_extras():
  directories = distribution_roots(_ROOT, [project.directory for project in PROJECTS])
  violations = [
    violation
    for directory in directories
    for violation in _sdk_violations(
      tomllib.loads((directory / 'pyproject.toml').read_text())['project']
    )
  ]
  assert violations == []


@pytest.mark.parametrize('group', _PROVIDER_WIRE_GROUPS)
@pytest.mark.parametrize('dependency', ['openai==3.33.0', 'anthropic', 'wire[client]>=1'])
def test_sdk_policy_rejects_required_sdks_and_transitive_client_extras(group, dependency):
  project = {
    'name': 'fixture',
    'entry-points': {group: {'fixture': 'fixture.declaration'}},
    'dependencies': [dependency],
  }
  assert _sdk_violations(project)
  project['dependencies'] = []
  project['optional-dependencies'] = {'client': [dependency]}
  assert _sdk_violations(project) == []
  project['optional-dependencies'] = {'other': [dependency]}
  assert _sdk_violations(project)
