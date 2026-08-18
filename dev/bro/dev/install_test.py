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
