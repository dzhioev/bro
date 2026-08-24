import subprocess

import pytest

from bro.workspace.human import HUMAN_EMAIL_ENV, HUMAN_NAME_ENV
from ride.identity import bro_git_identity_env, human_git_identity_env
from ride.repository import Repository


def test_identity_derives_from_the_bro_name():
  assert bro_git_identity_env('dev') == {
    'GIT_AUTHOR_NAME': 'dev',
    'GIT_AUTHOR_EMAIL': 'dev@bro',
    'GIT_COMMITTER_NAME': 'dev',
    'GIT_COMMITTER_EMAIL': 'dev@bro',
  }


@pytest.fixture
def repository(tmp_path, monkeypatch) -> Repository:
  """an attached checkout declaring no identity of its own, with the host's
  config out of reach so what a test sets is all git can see."""
  subprocess.run(['git', 'init', '-q', '-b', 'master', str(tmp_path)], check=True)
  monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'absent-global'))
  monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'absent-system'))
  return Repository(str(tmp_path), tmp_path)


class TestHumanIdentity:
  def test_the_attached_repository_names_the_human(self, repository):
    for key, value in (('user.name', 'Ada Lovelace'), ('user.email', 'ada@example.com')):
      subprocess.run(['git', 'config', key, value], cwd=repository.git_dir, check=True)
    assert human_git_identity_env(repository) == {
      HUMAN_NAME_ENV: 'Ada Lovelace',
      HUMAN_EMAIL_ENV: 'ada@example.com',
    }

  def test_a_detached_launch_names_no_human(self):
    assert human_git_identity_env(None) == {}

  def test_a_repository_declaring_no_identity_is_warned_about(self, repository, caplog):
    assert human_git_identity_env(repository) == {}
    assert 'credits no human' in caplog.text
