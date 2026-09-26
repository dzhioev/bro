"""code outside the credential resolver reads a credential by kind, through the
store's picks; a storage-addressed read resolves the one stored instance it
names whatever was picked, so it stays inside the resolver."""

import ast
import subprocess
from pathlib import Path

from bro.base import credentials

_ROOT = Path(__file__).resolve().parents[3]
_STORAGE_ADDRESSED = frozenset(
  {
    'get_instance',
    'get_instance_json',
    'try_get_instance',
    'available_instance',
    'resolve_instance',
  }
)
_RESOLVER = frozenset({'bro/base/credentials.py', 'bro/base/credentials_test.py'})


def _python_modules(root: Path) -> list[str]:
  listing = subprocess.run(
    ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '--', '*.py'],
    capture_output=True,
    text=True,
    check=True,
    cwd=root,
  ).stdout
  return [path for path in listing.splitlines() if (root / path).is_file()]


def _storage_addressed_reads(root: Path) -> list[str]:
  return sorted(
    f'{path}:{node.lineno}: {node.attr}'
    for path in _python_modules(root)
    if path not in _RESOLVER
    for node in ast.walk(ast.parse((root / path).read_text()))
    if isinstance(node, ast.Attribute) and node.attr in _STORAGE_ADDRESSED
  )


def test_storage_addressed_reads_stay_in_the_resolver():
  assert _storage_addressed_reads(_ROOT) == []


def test_the_policy_names_store_methods():
  assert all(callable(getattr(credentials.Store, name)) for name in _STORAGE_ADDRESSED)


def test_a_storage_addressed_read_outside_the_resolver_is_caught(tmp_path):
  (tmp_path / 'tool.py').write_text("print(store.get_instance('github'))\n")
  subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)

  assert _storage_addressed_reads(tmp_path) == ['tool.py:1: get_instance']
