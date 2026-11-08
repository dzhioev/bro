import subprocess
from pathlib import Path

import pytest

from bro.dev import markdown_policy


def _git(*args: str, cwd: Path) -> None:
  subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def origin(tmp_path, monkeypatch) -> Path:
  """a repository on branch `trunk` carrying `document.md`; the current
  directory is a clone of it."""
  origin = tmp_path / 'origin'
  origin.mkdir()
  _git('init', '-q', '-b', 'trunk', cwd=origin)
  _git('config', 'user.email', 't@t', cwd=origin)
  _git('config', 'user.name', 't', cwd=origin)
  (origin / 'document.md').write_text('One sentence.\n')
  _git('add', 'document.md', cwd=origin)
  _git('commit', '-q', '-m', 'seed', cwd=origin)
  _git('clone', '-q', str(origin), 'clone', cwd=tmp_path)
  monkeypatch.chdir(tmp_path / 'clone')
  return origin


class TestDefaultBase:
  def test_compares_against_the_default_branch_origin_names(self, origin, capsys):
    Path('document.md').write_text('Another sentence.\n')
    assert markdown_policy.main(['check-markdown']) == 1
    assert 'text changed' in capsys.readouterr().err

  def test_a_default_branch_renamed_after_the_clone_fails_until_fetched(self, origin, capsys):
    _git('branch', '-m', 'trunk', 'renamed', cwd=origin)
    Path('document.md').write_text('Another sentence.\n')
    assert markdown_policy.main(['check-markdown', 'document.md']) == 1
    assert 'origin/renamed names no commit' in capsys.readouterr().err
