"""what an install of the framework actually gets.

The test gate syncs every extra from the source tree, so neither an extra-only
import on a base-install surface nor a data file missing from the built
distribution shows up in-tree. The base-install cases re-run each console script
in a subprocess whose import system serves only the standard library and the
transitive closure of the base `dependencies` table; the data-file cases hold a
freshly built wheel against the tracked tree.
"""

import fnmatch
import importlib.metadata
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from bro.base import configs

_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_ENV = 'BASE_INSTALL_MODULES'
# `bro list` imports every persona any installed distribution declares, and every
# one of them lives under `bros`
_WORKSPACE_MODULES = {'bro', 'bros'}

# (console script, entry-point shim, arguments) — the environment scripts that
# belong to no extra
_SCRIPTS = [
  ('bro-shell-dir', 'bro_shell', []),
  ('credentials', 'bro_base_credentials', ['list']),
]

# data files the distribution deliberately leaves behind: development docs, and
# the host-provisioning scripts `setup_env.sh` drives from a checkout
_NOT_SHIPPED = [
  '*/CLAUDE.md',
  'bro/setup/setup_env.sh',
  'bro/setup/versions.sh',
  'bro/setup/ubuntu/*',
  'bro/setup/container/bump-claude-code.sh',
  'bro/setup/container/test_smoke.sh',
]

_GUARD = """
import importlib.abc
import importlib.machinery
import os
import sys
import sysconfig

_allowed = set(os.environ[{allowed_env!r}].split()) | sys.stdlib_module_names
_stdlib = [sysconfig.get_paths()['stdlib']]


class _BaseInstall(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path=None, target=None):
    root = fullname.partition('.')[0]
    # the interpreter's own build artifacts (`_sysconfigdata_<platform>`) are
    # named per platform and stay out of `stdlib_module_names`
    if root in _allowed or importlib.machinery.PathFinder.find_spec(root, _stdlib) is not None:
      return None
    raise ModuleNotFoundError(
      f'{{fullname}} is not importable on a base install ({{root}} ships with an extra)',
      name=root,
    )


sys.meta_path.insert(0, _BaseInstall())
"""


def _metadata() -> dict:
  return tomllib.loads((_ROOT / 'pyproject.toml').read_text())


def _own_bros() -> list[str]:
  """the personas this distribution declares — the only ones a base install has."""
  return sorted(_metadata()['project']['entry-points']['bro'])


def _requirement_name(requirement: str) -> str:
  """the distribution name a requirement string opens with."""
  head = requirement.split(';')[0].strip()
  end = 0
  while end < len(head) and (head[end].isalnum() or head[end] in '-_.'):
    end += 1
  name = head[:end]
  if name == '':
    raise ValueError(f'unparsable requirement {requirement!r}')
  return name.lower().replace('_', '-')


def _base_distributions() -> set[str]:
  """every distribution a bare `pip install bro` pulls in."""
  pending = [_requirement_name(item) for item in _metadata()['project']['dependencies']]
  resolved: set[str] = set()
  while len(pending) > 0:
    name = pending.pop()
    if name in resolved:
      continue
    resolved.add(name)
    requires = importlib.metadata.requires(name) or []
    # an `extra ==` marker gates the requirement on an extra of its own
    # distribution, which a base install never selects
    pending += [
      _requirement_name(item) for item in requires if 'extra ==' not in item.partition(';')[2]
    ]
  return resolved


def _base_modules() -> set[str]:
  distributions = _base_distributions()
  modules = {
    module
    for module, owners in importlib.metadata.packages_distributions().items()
    if any(_requirement_name(owner) in distributions for owner in owners)
  }
  return modules | _WORKSPACE_MODULES


def _run(script: str, entry_point: str, arguments: list[str], home: Path) -> str:
  program = _GUARD.format(allowed_env=_ALLOWED_ENV) + (
    f'\nsys.argv = {[script, *arguments]!r}\n'
    f'from bro._entrypoints import {entry_point}\n{entry_point}()\n'
  )
  environment = {
    key: value
    for key, value in os.environ.items()
    # the launching session's scoped credential store outranks the built-in
    # registry the scripts must run against
    if key not in ('CREDENTIALS_REGISTRY', 'BRO_CONFIGS_DIR')
  }
  environment['HOME'] = str(home)
  environment[_ALLOWED_ENV] = ' '.join(sorted(_base_modules()))
  result = subprocess.run(
    [sys.executable, '-c', program], capture_output=True, text=True, cwd=_ROOT, env=environment
  )
  command = ' '.join([script, *arguments])
  assert result.returncode == 0, f'`{command}` failed on a base install:\n{result.stderr}'
  return result.stdout


@pytest.mark.parametrize(('script', 'entry_point', 'arguments'), _SCRIPTS)
def test_script_runs_on_base_install(script, entry_point, arguments, tmp_path):
  _run(script, entry_point, arguments, tmp_path)


def test_list_runs_on_base_install(tmp_path):
  output = _run('bro', 'bro_run', ['list'], tmp_path)
  assert set(_own_bros()) <= {line.partition(':')[0] for line in output.splitlines()}


@pytest.mark.parametrize('name', _own_bros())
def test_show_runs_on_base_install(name, tmp_path):
  assert name in _run('bro', 'bro_run', ['show', name], tmp_path)


def _data_files() -> set[str]:
  """the package's non-source files — every candidate for shipping."""
  tracked = subprocess.run(
    ['git', 'ls-files', 'bro', 'bros'], capture_output=True, text=True, cwd=_ROOT, check=True
  ).stdout.split()
  return {path for path in tracked if not path.endswith('.py') and (_ROOT / path).is_file()}


def _expected_data_files() -> set[str]:
  return {
    path
    for path in _data_files()
    if not any(fnmatch.fnmatch(path, pattern) for pattern in _NOT_SHIPPED)
  }


@pytest.fixture(scope='module')
def shipped_files(tmp_path_factory) -> set[str]:
  """everything a freshly built wheel carries."""
  directory = tmp_path_factory.mktemp('wheel')
  subprocess.run(
    ['uv', 'build', '--wheel', str(_ROOT), '--out-dir', str(directory)],
    capture_output=True,
    cwd=_ROOT,
    check=True,
  )
  with zipfile.ZipFile(next(directory.glob('*.whl'))) as archive:
    return {name for name in archive.namelist() if not name.endswith('/')}


@pytest.fixture(scope='module')
def shipped_data_files(shipped_files) -> set[str]:
  return {name for name in shipped_files if not name.endswith('.py') and '.dist-info/' not in name}


def test_every_data_file_ships(shipped_data_files):
  missing = sorted(_expected_data_files() - shipped_data_files)
  assert missing == [], f'the wheel does not carry {missing}'


def test_the_wheel_carries_nothing_else(shipped_data_files):
  unexpected = sorted(shipped_data_files - _expected_data_files())
  assert unexpected == [], f'the wheel carries {unexpected}'


def test_the_wheel_is_the_distribution_the_framework_names(shipped_files):
  """the version `bro.base.configs` reads comes from the distribution built here."""
  built = {name.partition('.dist-info/')[0] for name in shipped_files if '.dist-info/' in name}
  assert built == {f'{configs.DISTRIBUTION}-{configs.VERSION}'}
  assert configs.__name__.replace('.', '/') + '.py' in shipped_files
