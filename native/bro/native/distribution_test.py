import configparser
import email.parser
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_PROJECTS = {
  'bro': _ROOT,
  'bro-benchmark': _ROOT / 'benchmark',
  'bro-dev': _ROOT / 'dev',
  'bro-native': _ROOT / 'native',
  'bro-ride': _ROOT / 'ride',
}
_NATIVE_DOMAIN = {
  'bro/fork.py',
  'bro/launch/call.py',
  'bro/launch/call_tui.py',
  'bro/launch/resume.py',
  'bro/launch/run.py',
  'bro/native/llm.py',
  'bro/native/llms/echo.py',
  'bro/native/llms/openai.py',
  'bro/native/providers.py',
  'bro/native/runner.py',
  'bro/run.py',
  'bro/trails/record/bro.py',
}
_NATIVE_SUPPORT = {
  'bro/native/__init__.py',
  'bro/native/_entrypoints.py',
  'bro/native/llms/__init__.py',
}
_CORE_DOMAIN = {
  'bro/bro.py',
  'bro/launch/broxy.py',
  'bro/launch/hold.py',
  'bro/launch/llm_flags.py',
  'bro/trails/record/spine.py',
}
_NAMESPACE_MODULES = {
  'bro.extra.github.api': 'bro/extra/github/api.py',
  'bro.extra.github.poll_pr': 'bro/extra/github/poll_pr.py',
  'bro.launch.call': 'bro/launch/call.py',
  'bro.launch.hold': 'bro/launch/hold.py',
  'bro.trails.record.bro': 'bro/trails/record/bro.py',
  'bro.trails.record.spine': 'bro/trails/record/spine.py',
}


def _metadata(wheel: Path):
  with zipfile.ZipFile(wheel) as archive:
    path = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
    return email.parser.Parser().parsestr(archive.read(path).decode())


@pytest.fixture(scope='module')
def wheels(tmp_path_factory) -> dict[str, Path]:
  output_directory = tmp_path_factory.mktemp('distribution-wheels')
  result = {}
  for expected_name, project in _PROJECTS.items():
    subprocess.run(
      ['uv', 'build', '--wheel', str(project), '--out-dir', str(output_directory)],
      capture_output=True,
      check=True,
    )
    matches = [
      wheel for wheel in output_directory.glob('*.whl') if _metadata(wheel)['Name'] == expected_name
    ]
    assert len(matches) == 1
    result[expected_name] = matches[0]
  return result


@pytest.fixture(scope='module')
def wheel_files(wheels) -> dict[str, set[str]]:
  result = {}
  for distribution, wheel in wheels.items():
    with zipfile.ZipFile(wheel) as archive:
      result[distribution] = {
        name for name in archive.namelist() if not name.endswith('/') and '.dist-info/' not in name
      }
  return result


def _entry_points(wheel: Path) -> configparser.ConfigParser:
  metadata = configparser.ConfigParser()
  with zipfile.ZipFile(wheel) as archive:
    path = next(name for name in archive.namelist() if name.endswith('.dist-info/entry_points.txt'))
    metadata.read_string(archive.read(path).decode())
  return metadata


def _requirement_name(requirement: str) -> str:
  return requirement.partition(';')[0].partition('[')[0].partition('=')[0].strip().lower()


def _project_dependencies(path: Path) -> set[str]:
  project = tomllib.loads(path.read_text())['project']
  return {_requirement_name(requirement) for requirement in project.get('dependencies', [])}


def _assert_namespace_modules(python_path: str) -> None:
  checks = ';'.join(
    f'assert importlib.util.find_spec({module!r}).origin.endswith({path!r})'
    for module, path in _NAMESPACE_MODULES.items()
  )
  environment = {**os.environ, 'PYTHONPATH': python_path}
  subprocess.run(
    [sys.executable, '-S', '-c', f'import importlib.util;{checks}'],
    cwd='/tmp',
    env=environment,
    capture_output=True,
    text=True,
    check=True,
  )


def test_native_domain_ships_only_in_bro_native(wheel_files):
  native_modules = {path for path in wheel_files['bro-native'] if path.endswith('.py')}
  assert native_modules == _NATIVE_DOMAIN | _NATIVE_SUPPORT
  assert _NATIVE_DOMAIN.isdisjoint(wheel_files['bro'])
  assert _CORE_DOMAIN <= wheel_files['bro']
  assert _CORE_DOMAIN.isdisjoint(wheel_files['bro-native'])


def test_distributions_do_not_ship_the_same_source_path(wheel_files):
  owners: dict[str, str] = {}
  for distribution, files in wheel_files.items():
    for path in files:
      assert path not in owners, f'{path} ships in both {owners.get(path)} and {distribution}'
      owners[path] = distribution


def test_native_distribution_owns_its_console_scripts(wheels):
  native_scripts = _entry_points(wheels['bro-native'])['console_scripts']
  core_scripts = _entry_points(wheels['bro'])['console_scripts']
  assert native_scripts['bro'] == 'bro.native._entrypoints:bro_run'
  assert native_scripts['bro.run'] == 'bro.native._entrypoints:bro_run'
  assert native_scripts['bro.native.llm'] == 'bro.native._entrypoints:bro_native_llm'
  assert {'bro', 'bro.run', 'bro.native.llm'}.isdisjoint(core_scripts)


def test_dependency_edges_follow_the_distribution_boundaries(wheels):
  native_requirements = {
    _requirement_name(requirement)
    for requirement in (_metadata(wheels['bro-native']).get_all('Requires-Dist') or [])
  }
  assert native_requirements == {
    'aiohttp',
    'bro',
    'humanize',
    'mcp',
    'openai',
    'rich',
    'textual',
    'trafilatura',
  }
  assert _project_dependencies(_ROOT / 'ride' / 'pyproject.toml') == {
    'bro',
    'humanize',
    'mcp',
    'rich',
    'textual',
  }
  assert _project_dependencies(_ROOT / 'dev' / 'pyproject.toml') == {'bro'}
  assert _project_dependencies(_ROOT / 'benchmark' / 'pyproject.toml') >= {'bro', 'bro-ride'}
  root = tomllib.loads((_ROOT / 'pyproject.toml').read_text())
  assert {'agent', 'ride'}.isdisjoint(root['project'].get('optional-dependencies', {}))


def test_shared_namespaces_resolve_from_editable_source_roots():
  _assert_namespace_modules(
    os.pathsep.join(str(_ROOT / directory) for directory in ('.', 'dev', 'native'))
  )


def test_shared_namespaces_resolve_after_wheel_install(wheels, tmp_path):
  for wheel in wheels.values():
    with zipfile.ZipFile(wheel) as archive:
      archive.extractall(tmp_path)
  _assert_namespace_modules(str(tmp_path))
