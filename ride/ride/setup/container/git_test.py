import os
import subprocess
from pathlib import Path

from bro.shell import SHELL_DIR

CONTAINER_DIR = Path(__file__).parent
GIT_SCRIPT = CONTAINER_DIR / 'git.sh'
LOG_SCRIPT = SHELL_DIR / 'log.sh'


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ['git', *args],
    cwd=cwd,
    check=check,
    capture_output=True,
    text=True,
    env={
      **os.environ,
      'GIT_AUTHOR_NAME': 'Test',
      'GIT_AUTHOR_EMAIL': 'test@example.com',
      'GIT_COMMITTER_NAME': 'Test',
      'GIT_COMMITTER_EMAIL': 'test@example.com',
    },
  )


def _initialize_repository(path: Path) -> None:
  path.mkdir()
  _git('init', '-q', '-b', 'master', cwd=path)


def _commit(path: Path, filename: str) -> str:
  (path / filename).write_text(filename)
  _git('add', filename, cwd=path)
  _git('commit', '-q', '-m', filename, cwd=path)
  return _git('rev-parse', 'HEAD', cwd=path).stdout.strip()


def _initialize_container_submodules(workspace: Path, host_repository: Path) -> None:
  subprocess.run(
    [
      'bash',
      '-e',
      '-c',
      'source "$1"; source "$2"; initialize_container_submodules "$3" "$4"',
      'container-git-test',
      str(LOG_SCRIPT),
      str(GIT_SCRIPT),
      str(workspace),
      str(host_repository),
    ],
    check=True,
    env={
      **os.environ,
      'BRO_LOG_LEVEL': 'WARNING',
      'GIT_CONFIG_COUNT': '1',
      'GIT_CONFIG_KEY_0': 'protocol.file.allow',
      'GIT_CONFIG_VALUE_0': 'always',
    },
  )


def test_recursive_fetch_uses_submodule_upstream_and_keeps_host_fallback(tmp_path):
  submodule_upstream = tmp_path / 'submodule-upstream'
  _initialize_repository(submodule_upstream)
  _commit(submodule_upstream, 'base')

  superproject_upstream = tmp_path / 'superproject-upstream'
  _initialize_repository(superproject_upstream)
  _git(
    '-c',
    'protocol.file.allow=always',
    'submodule',
    'add',
    '-q',
    str(submodule_upstream),
    'component',
    cwd=superproject_upstream,
  )
  _git('commit', '-q', '-m', 'base', cwd=superproject_upstream)

  host_repository = tmp_path / 'host-repository'
  _git('clone', '-q', str(superproject_upstream), str(host_repository), cwd=tmp_path)
  _git(
    '-c',
    'protocol.file.allow=always',
    'submodule',
    'update',
    '-q',
    '--init',
    cwd=host_repository,
  )

  workspace = tmp_path / 'workspace'
  _git('clone', '-q', '--shared', str(host_repository), str(workspace), cwd=tmp_path)
  _git('remote', 'set-url', 'origin', str(superproject_upstream), cwd=workspace)
  _git('remote', 'add', 'host', str(host_repository), cwd=workspace)
  _initialize_container_submodules(workspace, host_repository)

  _git('checkout', '-q', '-b', 'future', cwd=superproject_upstream)
  future_submodule_commit = _commit(submodule_upstream, 'future')
  _git('fetch', '-q', 'origin', cwd=superproject_upstream / 'component')
  _git('checkout', '-q', future_submodule_commit, cwd=superproject_upstream / 'component')
  _git('add', 'component', cwd=superproject_upstream)
  _git('commit', '-q', '-m', 'future', cwd=superproject_upstream)

  container_submodule = workspace / 'component'
  assert (
    _git(
      'cat-file',
      '-e',
      f'{future_submodule_commit}^{{commit}}',
      cwd=host_repository / 'component',
      check=False,
    ).returncode
    != 0
  )
  assert _git('remote', 'get-url', 'origin', cwd=container_submodule).stdout.strip() == str(
    submodule_upstream
  )
  assert _git('remote', 'get-url', 'host', cwd=container_submodule).stdout.strip() == str(
    host_repository / 'component'
  )

  _git('config', 'fetch.recurseSubmodules', 'on-demand', cwd=workspace)
  _git('fetch', 'origin', cwd=workspace)
  assert (
    _git('cat-file', '-t', future_submodule_commit, cwd=container_submodule).stdout.strip()
    == 'commit'
  )

  _git('checkout', '-q', '-b', 'host-only', cwd=host_repository / 'component')
  host_only_commit = _commit(host_repository / 'component', 'host-only')
  _git('fetch', 'host', cwd=container_submodule)
  assert (
    _git('cat-file', '-t', host_only_commit, cwd=container_submodule).stdout.strip() == 'commit'
  )


def test_bare_mirror_initializes_submodules_from_the_committed_url(tmp_path):
  submodule_upstream = tmp_path / 'submodule-upstream'
  _initialize_repository(submodule_upstream)
  _commit(submodule_upstream, 'base')

  superproject = tmp_path / 'superproject'
  _initialize_repository(superproject)
  _git(
    '-c',
    'protocol.file.allow=always',
    'submodule',
    'add',
    '-q',
    str(submodule_upstream),
    'component',
    cwd=superproject,
  )
  _git('commit', '-q', '-m', 'base', cwd=superproject)
  mirror = tmp_path / 'mirror.git'
  _git('clone', '-q', '--bare', str(superproject), str(mirror), cwd=tmp_path)
  workspace = tmp_path / 'workspace'
  _git('clone', '-q', str(mirror), str(workspace), cwd=tmp_path)
  _git('config', 'protocol.file.allow', 'always', cwd=workspace)

  _initialize_container_submodules(workspace, mirror)

  assert (workspace / 'component' / 'base').read_text() == 'base'
  assert _git('remote', 'get-url', 'origin', cwd=workspace / 'component').stdout.strip() == str(
    submodule_upstream
  )


def test_container_git_url_converts_github_ssh_to_https():
  result = subprocess.run(
    [
      'bash',
      '-e',
      '-c',
      'source "$1"; container_git_url "$2"',
      'container-git-test',
      str(GIT_SCRIPT),
      'git@github.com:owner/repository.git',
    ],
    check=True,
    capture_output=True,
    text=True,
  )
  assert result.stdout.strip() == 'https://github.com/owner/repository.git'
