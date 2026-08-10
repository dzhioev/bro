import bro.workspace.paths as workspace_paths


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
