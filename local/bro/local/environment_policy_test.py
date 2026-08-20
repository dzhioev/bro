"""no pytest run in this repository inherits the session that launched it.

`rebuild_environment` clears the framework's own namespaces, and holds only
while every variable the framework reads lives in one of those namespaces and
nothing makes a new one land there — so one test walks the sources and fails on
a name that is neither swept nor declared external below, leaving whoever
invents it to decide which it is.

The rebuild reaches a run only through the conftest at its pytest root, and this
repository has more than one such root — a project that ships from here without
being a workspace member configures pytest itself. So the other test holds every
root to applying it, the gap being invisible from inside a suite that has one.
"""

import ast
import os
import tomllib
from pathlib import Path

from bro.base.suite_environment_test_helper import (
  KEPT_VARIABLES,
  SESSION_NAMESPACES,
  SESSION_VARIABLES,
  rebuild_environment,
)

_ROOT = Path(__file__).resolve().parents[3]
_SOURCES = ('benchmark', 'bro', 'bros', 'dev', 'local', 'native', 'ride')

_REBUILD = rebuild_environment.__name__
_REBUILD_MODULE = rebuild_environment.__module__

# what the environment brings that is nobody's session state
_EXTERNAL = frozenset({'NO_COLOR', 'PAGER', 'XDG_DATA_HOME'})

_ENV_READERS = ('environ', 'getenv')
_ENV_CONSTANT_SUFFIXES = ('_ENV', '_VARIABLE')


def _swept(name: str) -> bool:
  return name.startswith(SESSION_NAMESPACES) or name in SESSION_VARIABLES


def _named_by(node: ast.AST) -> list[str]:
  """the env var names one syntax node spells out."""
  if isinstance(node, ast.Assign):
    # a module constant standing in for the name at its call sites
    if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
      return []
    named = any(
      isinstance(target, ast.Name) and target.id.endswith(_ENV_CONSTANT_SUFFIXES)
      for target in node.targets
    )
    return [node.value.value] if named else []
  if isinstance(node, ast.Subscript) and _reads_environ(node.value):
    index = node.slice
    return [index.value] if isinstance(index, ast.Constant) and isinstance(index.value, str) else []
  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
    if not _reads_environ(node.func.value) and node.func.attr not in _ENV_READERS:
      return []
    first = node.args[0] if len(node.args) > 0 else None
    return [first.value] if isinstance(first, ast.Constant) and isinstance(first.value, str) else []
  return []


def _reads_environ(node: ast.AST) -> bool:
  return isinstance(node, ast.Attribute) and node.attr in _ENV_READERS


def _sources() -> list[Path]:
  found: list[Path] = []
  for source in _SOURCES:
    found.extend(
      path
      for path in (_ROOT / source).rglob('*.py')
      # tests name variables they set themselves; the policy is about what the
      # framework reads from the environment it is handed. dot directories are
      # where a member's own `.venv` puts its third-party sources
      if not path.name.endswith(('_test.py', '_test_helper.py'))
      and path.name != 'conftest.py'
      and not any(part.startswith('.') for part in path.relative_to(_ROOT).parts)
    )
  return found


def test_every_read_variable_is_swept_or_external():
  undeclared: dict[str, list[str]] = {}
  for path in _sources():
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
      for name in _named_by(node):
        if _swept(name) or name in KEPT_VARIABLES or name in _EXTERNAL:
          continue
        undeclared.setdefault(name, []).append(str(path.relative_to(_ROOT)))
  assert undeclared == {}, (
    f'environment variables neither swept by {_REBUILD_MODULE} nor declared external: '
    f'{ {name: sorted(set(paths)) for name, paths in undeclared.items()} }'
  )


def _pytest_roots() -> list[Path]:
  """every directory a pytest run in this repository roots at."""
  roots: list[Path] = []
  for directory, subdirectories, files in os.walk(_ROOT):
    # dot directories are where a project's own `.venv` puts its third-party
    # sources, each carrying pyproject files of its own
    subdirectories[:] = [name for name in subdirectories if not name.startswith('.')]
    if 'pyproject.toml' not in files:
      continue
    metadata = tomllib.loads((Path(directory) / 'pyproject.toml').read_text())
    if 'pytest' in metadata.get('tool', {}):
      roots.append(Path(directory))
  return roots


def _rebuilds_the_environment(conftest: Path) -> bool:
  if not conftest.is_file():
    return False
  tree = ast.parse(conftest.read_text())
  return any(
    isinstance(node, ast.ImportFrom) and node.module == _REBUILD_MODULE for node in ast.walk(tree)
  ) and any(
    isinstance(node, ast.Call) and ast.unparse(node.func).rsplit('.', 1)[-1] == _REBUILD
    for node in ast.walk(tree)
  )


def test_every_pytest_root_rebuilds_the_environment():
  inheriting = [
    str(root.relative_to(_ROOT))
    for root in _pytest_roots()
    if not _rebuilds_the_environment(root / 'conftest.py')
  ]
  assert inheriting == [], (
    f'pytest roots whose conftest does not call {_REBUILD_MODULE}.{_REBUILD}, so their '
    f'suite inherits the session that launched it: {inheriting}'
  )
