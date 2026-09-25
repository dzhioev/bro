import pytest

from bro.base.git_url import (
  git_url_path,
  is_git_url,
  is_network_git_url,
  normalize_git_url,
  sanitize_git_url,
)


class TestRecognition:
  def test_scheme_and_scp_urls_are_recognized(self):
    assert is_git_url('https://github.com/Owner/Repo.git')
    assert is_git_url('git@github.com:Owner/Repo.git')
    assert not is_git_url('repository-name')
    assert not is_git_url('/home/me/repository')


class TestNormalization:
  def test_scheme_host_and_trailing_slash_stabilize(self):
    assert normalize_git_url('HTTPS://GitHub.COM/Owner/Repo.git/') == (
      normalize_git_url('https://github.com/Owner/Repo.git')
    )

  def test_scp_host_is_normalized(self):
    assert normalize_git_url('git@GitHub.COM:Owner/Repo.git/') == 'git@github.com:Owner/Repo.git'

  def test_a_non_url_is_rejected(self):
    with pytest.raises(ValueError, match='not a git URL'):
      normalize_git_url('/home/me/repository')


class TestRecordingSanitizer:
  def test_scheme_credentials_query_and_fragment_are_removed(self):
    assert (
      sanitize_git_url('HTTPS://user:token@GitHub.COM/Owner/Repo.git?token=secret#ref')
      == 'https://github.com/Owner/Repo.git'
    )

  def test_scp_user_is_kept_while_query_and_fragment_are_removed(self):
    assert (
      sanitize_git_url('git@GitHub.COM:Owner/Repo.git?token=secret#ref')
      == 'git@github.com:Owner/Repo.git'
    )

  def test_only_network_remotes_are_recognized_for_the_recorded_url(self):
    assert is_network_git_url('https://github.com/Owner/Repo.git')
    assert is_network_git_url('git@github.com:Owner/Repo.git')
    assert not is_network_git_url('/home/me/repository')
    assert not is_network_git_url('file:///home/me/repository')


class TestPath:
  def test_the_repository_path_comes_out_of_either_shape(self):
    assert git_url_path('https://github.com/owner/repo.git') == '/owner/repo.git'
    assert git_url_path('git@github.com:owner/repo.git') == 'owner/repo.git'
