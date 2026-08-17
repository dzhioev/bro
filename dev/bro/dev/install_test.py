import os
import subprocess
from pathlib import Path

import bro.dev.install as install
from bro.dev.install import install_repository
from bro.workspace.paths import project_key


def test_installs_hooks_git_alias_and_runtime_root(monkeypatch, tmp_path):
  repository = tmp_path / 'repository'
  repository.mkdir()
  subprocess.run(['git', 'init', '-q', str(repository)], check=True)
  runtime_base = tmp_path / 'runtime'
  calls = []
  real_run = subprocess.run

  def run(command, *args, **kwargs):
    if command[0] == 'sudo':
      calls.append(command)
      Path(command[-1]).mkdir(parents=True, exist_ok=True)
      return subprocess.CompletedProcess(command, 0)
    return real_run(command, *args, **kwargs)

  monkeypatch.setattr('bro.workspace.paths.RUNTIME_BASE', runtime_base)
  monkeypatch.setattr(install, 'in_container', lambda: False)
  monkeypatch.setattr(install.subprocess, 'run', run)

  install_repository(repository)

  assert calls[-1][-1] == str(runtime_base / project_key(repository))
  for hook_name in ('commit-msg', 'post-commit'):
    hook = repository / '.git' / 'hooks' / hook_name
    assert hook.read_text().startswith('#!/usr/bin/env -S bash -e\n')
    assert os.access(hook, os.X_OK)
  alias = real_run(
    ['git', '-C', str(repository), 'config', '--local', '--get', 'alias.golc'],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()
  assert alias == '!bro.dev.git-golc'


def test_existing_runtime_root_needs_no_privilege(monkeypatch, tmp_path):
  repository = tmp_path / 'repository'
  repository.mkdir()
  subprocess.run(['git', 'init', '-q', str(repository)], check=True)
  runtime_base = tmp_path / 'runtime'
  (runtime_base / project_key(repository)).mkdir(parents=True)

  monkeypatch.setattr('bro.workspace.paths.RUNTIME_BASE', runtime_base)
  monkeypatch.setattr(install, 'in_container', lambda: False)

  install_repository(repository)
