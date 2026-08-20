import subprocess
from pathlib import Path

import pytest

from ride.repository import (
  clean_managed_mirrors,
  is_git_url,
  mirror_key,
  normalize_git_url,
  open_repository,
  resolve_repository,
)


def _git(path: Path, *args: str) -> str:
  return subprocess.check_output(['git', *args], cwd=path, text=True).strip()


def _upstream(tmp_path: Path) -> tuple[Path, Path, str]:
  tmp_path.mkdir(parents=True, exist_ok=True)
  work = tmp_path / 'work'
  upstream = tmp_path / 'upstream.git'
  subprocess.run(['git', 'init', '-q', '-b', 'main', work], check=True)
  _git(work, 'config', 'user.email', 'test@example.com')
  _git(work, 'config', 'user.name', 'Test')
  (work / 'pyproject.toml').write_text('[tool.bro]\ndefault = "bro-dev"\n')
  (work / 'value.txt').write_text('one\n')
  _git(work, 'add', '.')
  _git(work, 'commit', '-qm', 'first')
  subprocess.run(['git', 'clone', '-q', '--bare', work, upstream], check=True)
  _git(work, 'remote', 'add', 'origin', str(upstream))
  return work, upstream, _git(work, 'rev-parse', 'HEAD')


class TestUrlRecognition:
  def test_scheme_and_scp_urls_are_recognized(self):
    assert is_git_url('https://github.com/Owner/Repo.git')
    assert is_git_url('git@github.com:Owner/Repo.git')
    assert not is_git_url('repository-name')

  def test_normalization_stabilizes_scheme_host_and_trailing_slash(self):
    first = normalize_git_url('HTTPS://GitHub.COM/Owner/Repo.git/')
    second = normalize_git_url('https://github.com/Owner/Repo.git')
    assert first == second == 'https://github.com/Owner/Repo.git'
    assert mirror_key(first) == mirror_key(second)
    assert mirror_key(first).startswith('owner-repo-')

  def test_scp_host_is_normalized(self):
    assert normalize_git_url('git@GitHub.COM:Owner/Repo.git/') == 'git@github.com:Owner/Repo.git'

  def test_non_path_non_url_errors_crisply(self):
    with pytest.raises(ValueError, match='existing checkout path or a git URL'):
      resolve_repository('repository-name')


class TestManagedMirror:
  def test_clone_fetch_and_committed_tree_reads(self, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    work, upstream, first_commit = _upstream(tmp_path)
    url = upstream.as_uri()

    first = resolve_repository(url)
    assert first.identity == url
    assert first.is_url
    assert first.default_base == first_commit
    assert first.read_file('value.txt') == b'one\n'
    assert first.project_config().default_bro == 'bro-dev'
    assert _git(first.git_dir, 'config', '--get', 'gc.auto') == '0'
    assert _git(first.git_dir, 'config', '--get', 'remote.origin.url') == url
    assert _git(first.git_dir, 'symbolic-ref', 'refs/remotes/origin/HEAD') == (
      'refs/remotes/origin/main'
    )
    assert _git(first.git_dir, 'symbolic-ref', 'HEAD') == 'refs/remotes/origin/main'

    (work / 'value.txt').write_text('two\n')
    _git(work, 'commit', '-qam', 'second')
    _git(work, 'push', '-q', 'origin', 'main')
    second_commit = _git(work, 'rev-parse', 'HEAD')

    second = resolve_repository(url)
    assert second.git_dir == first.git_dir
    assert second.default_base == second_commit
    assert second.read_file('value.txt') == b'two\n'
    assert _git(second.git_dir, 'cat-file', '-t', first_commit) == 'commit'

  def test_fetch_never_prunes_disappeared_refs(self, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    work, upstream, commit = _upstream(tmp_path)
    _git(work, 'branch', 'retained')
    _git(work, 'push', '-q', 'origin', 'retained')
    repository = resolve_repository(upstream.as_uri())
    assert _git(repository.git_dir, 'rev-parse', 'refs/remotes/origin/retained') == commit

    _git(work, 'push', '-q', 'origin', '--delete', 'retained')
    repository = resolve_repository(upstream.as_uri())
    assert _git(repository.git_dir, 'rev-parse', 'refs/remotes/origin/retained') == commit

  def test_open_uses_the_existing_mirror_without_fetching(self, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    _, upstream, commit = _upstream(tmp_path)
    resolved = resolve_repository(upstream.as_uri())
    upstream.rename(tmp_path / 'offline.git')

    opened = open_repository(resolved.identity)
    assert opened.git_dir == resolved.git_dir
    assert opened.default_base == commit

  def test_cleanup_keeps_referenced_mirrors(self, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    _, first_upstream, _ = _upstream(tmp_path / 'first')
    _, second_upstream, _ = _upstream(tmp_path / 'second')
    first = resolve_repository(first_upstream.as_uri())
    second = resolve_repository(second_upstream.as_uri())

    assert clean_managed_mirrors({first.identity}) == (1, 1)
    assert first.git_dir.is_dir()
    assert not second.git_dir.exists()
