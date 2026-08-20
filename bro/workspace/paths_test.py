import stat
import subprocess

import pytest

import bro.workspace.paths as workspace_paths


def test_no_git_on_path_names_no_project(monkeypatch):
  def absent(*args, **kwargs):
    raise FileNotFoundError('git')

  monkeypatch.setattr(workspace_paths, 'git_run', absent)

  assert workspace_paths.find_project_root() is None
  with pytest.raises(workspace_paths.RuntimeLocationError, match='no git repository'):
    workspace_paths.project_root()


def test_a_directory_outside_a_repository_names_no_project(monkeypatch, tmp_path):
  monkeypatch.chdir(tmp_path)
  monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))

  assert workspace_paths.find_project_root() is None


def test_a_repository_names_its_own_root(monkeypatch, tmp_path):
  monkeypatch.chdir(tmp_path)
  subprocess.run(['git', 'init', '--quiet'], cwd=tmp_path, check=True)

  assert workspace_paths.find_project_root() == tmp_path.resolve()


def test_linked_worktree_names_the_main_checkout(monkeypatch, tmp_path):
  repository = tmp_path / 'repository'
  worktree = tmp_path / 'worktree'
  subprocess.run(['git', 'init', '--quiet', str(repository)], check=True)
  subprocess.run(
    [
      'git',
      '-C',
      str(repository),
      '-c',
      'user.name=test',
      '-c',
      'user.email=test@example.invalid',
      'commit',
      '--allow-empty',
      '-m',
      'root',
    ],
    capture_output=True,
    check=True,
  )
  subprocess.run(
    ['git', '-C', str(repository), 'worktree', 'add', '-q', '-b', 'worktree-test', str(worktree)],
    check=True,
  )

  assert workspace_paths.find_project_root(worktree) == repository.resolve()


def test_the_data_home_names_the_runtime_base(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  assert workspace_paths.runtime_base() == tmp_path / 'ride'

  monkeypatch.delenv('XDG_DATA_HOME')
  monkeypatch.setattr(workspace_paths.Path, 'home', lambda: tmp_path)
  assert workspace_paths.runtime_base() == tmp_path / '.local' / 'share' / 'ride'


def test_a_relative_data_home_is_refused(monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', 'share')
  with pytest.raises(workspace_paths.RuntimeLocationError, match='absolute'):
    workspace_paths.runtime_base()


def test_runtime_paths_share_the_flat_root():
  root = workspace_paths.runtime_base()
  assert workspace_paths.workspaces_dir() == root / 'workspaces'
  assert workspace_paths.broker_dir() == root / 'broker'
  assert workspace_paths.summon_dir() == root / 'summon'
  assert workspace_paths.trails_dir() == root / 'trails'


def test_container_trails_use_the_absolute_mount(monkeypatch):
  monkeypatch.setenv('RIDE_IN_CONTAINER', '1')
  assert workspace_paths.trails_dir() == workspace_paths.CONTAINER_TRAILS_ROOT


def test_the_runtime_root_is_created_private_on_first_use(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
  root = workspace_paths.ensure_runtime_root()

  assert root == workspace_paths.runtime_base()
  assert stat.S_IMODE(root.stat().st_mode) == 0o700
  assert workspace_paths.ensure_runtime_root() == root


def _workspaces_dir(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  workspaces = workspace_paths.workspaces_dir()
  workspaces.mkdir(parents=True)
  return workspaces


def test_fresh_workspace_name_is_unique_per_call(monkeypatch, tmp_path):
  _workspaces_dir(monkeypatch, tmp_path)
  first = workspace_paths.fresh_workspace_name('ask-dev')
  second = workspace_paths.fresh_workspace_name('ask-dev')
  assert first.startswith('ask-dev-')
  assert first != second


def test_fresh_workspace_name_regenerates_on_collision(monkeypatch, tmp_path):
  workspaces = _workspaces_dir(monkeypatch, tmp_path)
  suffixes = iter(['aaaaaa', 'bbbbbb'])
  monkeypatch.setattr(workspace_paths.secrets, 'token_hex', lambda _: next(suffixes))
  (workspaces / 'idea-aaaaaa').mkdir()
  assert workspace_paths.fresh_workspace_name('idea') == 'idea-bbbbbb'
