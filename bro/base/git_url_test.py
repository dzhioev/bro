import pytest

from bro.base.git_url import (
  canonical_github_url,
  git_url_path,
  github_repository,
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


class TestGitHub:
  @pytest.mark.parametrize(
    'url',
    [
      'git@github.com:Owner/Repo.git',
      'git@GitHub.com:Owner/Repo',
      'ssh://git@github.com/Owner/Repo.git',
      'ssh://git@github.com:22/Owner/Repo',
      'https://github.com/Owner/Repo.git/',
      'https://x-access-token@github.com/Owner/Repo',
      'http://github.com/Owner/Repo',
    ],
  )
  def test_every_spelling_maps_to_one_https_url(self, url):
    assert github_repository(url) == 'Owner/Repo'
    assert canonical_github_url(url) == 'https://github.com/Owner/Repo'

  @pytest.mark.parametrize(
    'url',
    [
      'git@gitlab.com:Owner/Repo.git',
      'https://github.example.com/Owner/Repo.git',
      'ssh://git@gitlab.com/github.com/Repo.git',
      'file:///srv/github.com/Owner/Repo.git',
      'file://github.com/Owner/Repo.git',
      'git://github.com/Owner/Repo.git',
    ],
  )
  def test_another_host_or_transport_is_not_github(self, url):
    assert github_repository(url) is None
    assert canonical_github_url(url) is None

  @pytest.mark.parametrize(
    'url', ['https://github.com/Owner', 'git@github.com:Owner/Repo/tree', 'https://github.com/']
  )
  def test_a_github_url_naming_no_repository_is_rejected(self, url):
    with pytest.raises(ValueError, match='names no owner/name repository'):
      github_repository(url)
