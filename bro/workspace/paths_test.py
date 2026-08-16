import subprocess

import pytest

import bro.workspace.paths as workspace_paths


def test_no_git_on_path_names_no_project(monkeypatch):
  def absent(*args, **kwargs):
    raise FileNotFoundError('git')

  monkeypatch.setattr(workspace_paths, 'git_run', absent)

  assert workspace_paths.find_project_root() is None
  with pytest.raises(ValueError, match='no git repository'):
    workspace_paths.project_root()


def test_a_directory_outside_a_repository_names_no_project(monkeypatch, tmp_path):
  monkeypatch.chdir(tmp_path)
  monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))

  assert workspace_paths.find_project_root() is None


def test_a_repository_names_its_own_root(monkeypatch, tmp_path):
  monkeypatch.chdir(tmp_path)
  subprocess.run(['git', 'init', '--quiet'], cwd=tmp_path, check=True)

  assert workspace_paths.find_project_root() == tmp_path.resolve()


def _workspaces_dir(monkeypatch, tmp_path):
  monkeypatch.setattr(workspace_paths, 'project_root', lambda: tmp_path)
  workspaces = workspace_paths.workspaces_dir(tmp_path)
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
