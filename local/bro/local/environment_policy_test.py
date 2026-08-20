"""every environment variable the sources read is either swept from the test
environment or declared external.

`conftest.py` clears the framework's own namespaces so the suite never inherits
the session that launched it. That holds only while every variable the framework
reads lives in one of those namespaces, and nothing makes a new one land there —
so this walks the sources and fails on a name that is neither swept nor named
below, leaving whoever invents it to decide which it is.
"""

import ast
from pathlib import Path

from conftest import KEPT_VARIABLES, SESSION_NAMESPACES, SESSION_VARIABLES

_ROOT = Path(__file__).resolve().parents[3]
_SOURCES = ('benchmark', 'bro', 'bros', 'dev', 'local', 'native', 'ride')

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
    "environment variables neither swept by conftest's sweep nor declared external: "
    f'{ {name: sorted(set(paths)) for name, paths in undeclared.items()} }'
  )
