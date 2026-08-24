import subprocess

import pytest

from bro.workspace.human import (
  HUMAN_EMAIL_ENV,
  HUMAN_NAME_ENV,
  Human,
  configured_human,
  human_env,
  session_human,
)


@pytest.fixture
def repository(tmp_path, monkeypatch):
  """a checkout declaring no identity of its own, with the host's config out of
  reach so what a test sets is all git can see."""
  subprocess.run(['git', 'init', '-q', '-b', 'master', str(tmp_path)], check=True)
  monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'absent-global'))
  monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'absent-system'))
  return tmp_path


def _configure(repository, key: str, value: str) -> None:
  subprocess.run(['git', 'config', key, value], cwd=repository, check=True)


class TestConfiguredHuman:
  def test_a_declared_identity_is_the_human(self, repository):
    _configure(repository, 'user.name', 'Ada Lovelace')
    _configure(repository, 'user.email', 'ada@example.com')
    assert configured_human(repository) == Human('Ada Lovelace', 'ada@example.com')

  def test_a_checkout_declaring_nothing_has_no_human(self, repository):
    assert configured_human(repository) is None

  def test_a_half_declared_identity_has_no_human(self, repository):
    _configure(repository, 'user.name', 'Ada Lovelace')
    assert configured_human(repository) is None

  def test_a_failing_git_raises(self, tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'absent-global'))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'absent-system'))
    (tmp_path / '.git').write_text('gitdir: nowhere\n')
    with pytest.raises(RuntimeError, match='git config --get user.name failed'):
      configured_human(tmp_path)


class TestSessionHuman:
  def test_the_launch_environment_names_the_human(self, monkeypatch):
    for variable, value in human_env(Human('Ada Lovelace', 'ada@example.com')).items():
      monkeypatch.setenv(variable, value)
    assert session_human() == Human('Ada Lovelace', 'ada@example.com')

  def test_a_launch_naming_none_has_no_human(self):
    assert session_human() is None

  def test_half_an_identity_stops_the_caller(self, monkeypatch):
    monkeypatch.setenv(HUMAN_NAME_ENV, 'Ada Lovelace')
    with pytest.raises(RuntimeError, match=HUMAN_EMAIL_ENV):
      session_human()
