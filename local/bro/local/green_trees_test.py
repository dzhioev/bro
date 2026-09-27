import subprocess

import pytest

from bro.local import green_trees


def git(root, *arguments):
  return subprocess.run(
    ('git', *arguments), cwd=root, check=True, capture_output=True, text=True
  ).stdout.strip()


def head_tree(root):
  return git(root, 'rev-parse', 'HEAD^{tree}')


@pytest.fixture
def checkout(tmp_path):
  git(tmp_path, 'init', '-b', 'main')
  git(tmp_path, 'config', 'user.email', 'probe@example.com')
  git(tmp_path, 'config', 'user.name', 'probe')
  (tmp_path / 'module.py').write_text('VALUE = 1\n')
  git(tmp_path, 'add', '.')
  git(tmp_path, 'commit', '-m', 'first')
  return tmp_path


def test_a_clean_worktree_names_the_head_tree(checkout):
  assert green_trees.clean_tree(checkout) == head_tree(checkout)


@pytest.mark.parametrize('path', ['module.py', 'untracked.py', 'package/untracked.py'])
def test_a_worktree_carrying_anything_beside_its_head_names_no_tree(checkout, path):
  (checkout / path).parent.mkdir(exist_ok=True)
  (checkout / path).write_text('VALUE = 2\n')

  assert green_trees.clean_tree(checkout) is None


def test_a_tree_nothing_passed_on_holds_no_stage(checkout):
  assert green_trees.passed(checkout, head_tree(checkout)) == frozenset()


def test_a_recorded_stage_holds_for_every_commit_of_its_tree(checkout):
  tree = head_tree(checkout)
  green_trees.record(checkout, tree, 'unit', 'whole')
  green_trees.record(checkout, tree, 'broker_e2e', 'shard 2/3')
  git(checkout, 'commit', '--amend', '-m', 'first, reworded')

  assert green_trees.passed(checkout, head_tree(checkout)) == {
    ('unit', 'whole'),
    ('broker_e2e', 'shard 2/3'),
  }


def test_a_changed_tree_holds_none_of_its_parent_stages(checkout):
  green_trees.record(checkout, head_tree(checkout), 'unit', 'whole')
  (checkout / 'module.py').write_text('VALUE = 2\n')
  git(checkout, 'commit', '-am', 'second')

  assert green_trees.passed(checkout, head_tree(checkout)) == frozenset()
