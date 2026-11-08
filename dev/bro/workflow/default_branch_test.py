import subprocess
from pathlib import Path

import pytest

from bro.workflow import default_branch


def _git(*args: str, cwd: Path) -> str:
  return subprocess.run(
    ['git', *args], cwd=cwd, check=True, capture_output=True, text=True
  ).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch) -> Path:
  """a repository on branch `trunk`; the current directory is a clone of it."""
  origin = tmp_path / 'origin'
  origin.mkdir()
  _git('init', '-q', '-b', 'trunk', cwd=origin)
  _git('config', 'user.email', 't@t', cwd=origin)
  _git('config', 'user.name', 't', cwd=origin)
  _git('commit', '-q', '--allow-empty', '-m', 'seed', cwd=origin)
  _git('clone', '-q', str(origin), 'clone', cwd=tmp_path)
  monkeypatch.chdir(tmp_path / 'clone')
  return origin


def test_reads_the_branch_origin_names_as_its_head(origin):
  assert default_branch.read() == 'trunk'


def test_follows_the_remote_past_the_clone_time_origin_head(origin):
  _git('checkout', '-q', '-b', 'renamed', cwd=origin)
  assert _git('symbolic-ref', '--short', 'refs/remotes/origin/HEAD', cwd=Path.cwd()) == (
    'origin/trunk'
  )
  assert default_branch.read() == 'renamed'


def test_a_detached_origin_head_names_no_branch(origin):
  _git('checkout', '-q', '--detach', cwd=origin)
  with pytest.raises(default_branch.DefaultBranchError, match='no branch'):
    default_branch.read()


def test_an_unreachable_origin_fails(origin, tmp_path):
  _git('remote', 'set-url', 'origin', str(tmp_path / 'missing'), cwd=Path.cwd())
  with pytest.raises(default_branch.DefaultBranchError, match='ls-remote'):
    default_branch.read()


def test_the_command_prints_the_branch(origin, capsys):
  assert default_branch.main(['default-branch']) is None
  assert capsys.readouterr().out == 'trunk\n'


def test_the_command_exits_nonzero_when_origin_names_no_branch(origin, capsys):
  _git('checkout', '-q', '--detach', cwd=origin)
  assert default_branch.main(['default-branch']) == 1
  assert capsys.readouterr().out == ''
