"""Git repositories for delivery command tests."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class Repository:
  path: Path
  remote: Path

  def git(self, *arguments: str) -> str:
    return subprocess.check_output(['git', '-C', str(self.path), *arguments], text=True).strip()

  def commit(self, name: str, content: str) -> str:
    (self.path / name).write_text(content)
    self.git('add', name)
    self.git('commit', '-qm', f'change {name}')
    return self.git('rev-parse', 'HEAD')


@pytest.fixture
def repository(tmp_path, monkeypatch):
  remote, path = tmp_path / 'remote.git', tmp_path / 'repo'
  subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
  subprocess.run(['git', 'init', '-q', '-b', 'main', str(path)], check=True)
  repo = Repository(path, remote)
  repo.git('config', 'user.name', 'tester')
  repo.git('config', 'user.email', 'tester@example.com')
  repo.commit('seed', 'seed\n')
  repo.git('remote', 'add', 'origin', str(remote))
  repo.git('push', '-q', 'origin', 'main')
  repo.git('checkout', '-qb', 'feature')
  repo.commit('feature', 'feature\n')
  monkeypatch.chdir(path)
  return repo
