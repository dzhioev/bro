import os
import subprocess

from bro.dev.install import install_repository


def test_installs_hooks_and_the_git_alias(tmp_path):
  repository = tmp_path / 'repository'
  repository.mkdir()
  subprocess.run(['git', 'init', '-q', str(repository)], check=True)

  install_repository(repository)

  for hook_name in ('commit-msg', 'post-commit'):
    hook = repository / '.git' / 'hooks' / hook_name
    assert hook.read_text().startswith('#!/usr/bin/env -S bash -e\n')
    assert os.access(hook, os.X_OK)
  alias = subprocess.run(
    ['git', '-C', str(repository), 'config', '--local', '--get', 'alias.golc'],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  assert alias == '!bro.dev.git-golc'


def _blame_ignore_setting(repository) -> str:
  finished = subprocess.run(
    ['git', '-C', str(repository), 'config', '--local', '--get', 'blame.ignoreRevsFile'],
    capture_output=True,
    text=True,
  )
  return finished.stdout.strip()


def test_a_blame_ignore_list_is_configured_when_the_repository_carries_one(tmp_path):
  repository = tmp_path / 'repository'
  repository.mkdir()
  subprocess.run(['git', 'init', '-q', str(repository)], check=True)
  (repository / '.git-blame-ignore-revs').write_text('')

  install_repository(repository)

  assert _blame_ignore_setting(repository) == '.git-blame-ignore-revs'


def test_no_blame_ignore_list_leaves_blame_unconfigured(tmp_path):
  repository = tmp_path / 'repository'
  repository.mkdir()
  subprocess.run(['git', 'init', '-q', str(repository)], check=True)

  install_repository(repository)

  assert _blame_ignore_setting(repository) == ''
