import subprocess
from pathlib import Path

import pytest

from bro.dev.affected_tests import changed_paths, import_graph, module_name, reachable


def _write(root: Path, path: str, text: str = '') -> Path:
  full = root / path
  full.parent.mkdir(parents=True, exist_ok=True)
  full.write_text(text)
  return full


@pytest.fixture
def repository(tmp_path):
  _write(tmp_path, 'thing/__init__.py')
  _write(tmp_path, 'thing/base.py')
  _write(tmp_path, 'thing/store.py', 'from thing import base\n')
  _write(tmp_path, 'thing/store_test.py', 'import thing.store\n')
  _write(tmp_path, 'thing/api.py', 'from thing.store import Store\n')
  _write(tmp_path, 'thing/api_test.py', 'from thing import api\n')
  _write(tmp_path, 'thing/lonely_test.py', 'import json\n')
  _write(tmp_path, 'member/thing/extra.py', 'import thing.base\n')
  _write(tmp_path, 'member/thing/extra_test.py', 'from thing import extra\n')
  return tmp_path


def test_the_innermost_source_root_names_a_module(repository):
  roots = (repository, repository / 'member')

  assert module_name(roots, repository / 'member/thing/extra.py') == 'thing.extra'
  assert module_name(roots, repository / 'thing/store.py') == 'thing.store'
  assert module_name(roots, repository / 'thing/__init__.py') == 'thing'
  assert module_name(roots, repository / 'thing/notes.md') is None
  assert module_name(roots, Path('/elsewhere/thing.py')) is None


def test_a_change_reaches_everything_that_imports_it(repository):
  importers = import_graph(repository, (repository, repository / 'member'))

  assert reachable(importers, ['thing.store']) == {
    'thing.store',
    'thing.store_test',
    'thing.api',
    'thing.api_test',
  }


def test_a_package_change_reaches_its_submodules_importers(repository):
  importers = import_graph(repository, (repository, repository / 'member'))

  # `import thing.base` executes `thing` too, so its importers see the change
  assert 'thing.extra' in reachable(importers, ['thing'])
  assert 'thing.lonely_test' not in reachable(importers, ['thing.base'])


def test_relative_imports_resolve_against_their_package(tmp_path):
  _write(tmp_path, 'thing/__init__.py')
  _write(tmp_path, 'thing/base.py')
  _write(tmp_path, 'thing/near.py', 'from . import base\n')
  _write(tmp_path, 'thing/inner/__init__.py')
  _write(tmp_path, 'thing/inner/far.py', 'from .. import base\n')

  importers = import_graph(tmp_path, (tmp_path,))

  assert reachable(importers, ['thing.base']) == {'thing.base', 'thing.near', 'thing.inner.far'}


def test_the_diff_covers_commits_working_tree_and_untracked_files(tmp_path):
  def git(*arguments: str) -> None:
    subprocess.run(('git', *arguments), cwd=tmp_path, check=True, capture_output=True)

  git('init', '-b', 'main')
  git('config', 'user.email', 'gate@example.com')
  git('config', 'user.name', 'gate')
  _write(tmp_path, 'kept.py')
  git('add', '.')
  git('commit', '-m', 'base')
  git('branch', 'base')
  _write(tmp_path, 'committed.py')
  git('add', '.')
  git('commit', '-m', 'work')
  _write(tmp_path, 'kept.py', 'edited\n')
  _write(tmp_path, 'untracked.py')

  assert changed_paths(tmp_path, 'base') == ['committed.py', 'kept.py', 'untracked.py']
