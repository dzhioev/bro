import os
import subprocess

from bro.dev.install import install_repository


def test_installs_hooks_and_git_alias(tmp_path):
  subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)

  install_repository(tmp_path)

  for hook_name in ('commit-msg', 'post-commit'):
    hook = tmp_path / '.git' / 'hooks' / hook_name
    assert hook.read_text().startswith('#!/usr/bin/env -S bash -e\n')
    assert os.access(hook, os.X_OK)
  alias = subprocess.run(
    ['git', '-C', str(tmp_path), 'config', '--local', '--get', 'alias.golc'],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  assert alias == '!bro.dev.git-golc'
