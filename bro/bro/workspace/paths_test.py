import cw.paths


def _workspace_dirs(monkeypatch, tmp_path):
  monkeypatch.setattr(cw.paths, '_project_root', lambda: tmp_path)
  worktrees = tmp_path / 'var' / 'cw' / 'worktrees'
  containers = tmp_path / 'var' / 'cw' / 'containers'
  worktrees.mkdir(parents=True)
  containers.mkdir(parents=True)
  return worktrees, containers


def test_fresh_workspace_name_is_unique_per_call(monkeypatch, tmp_path):
  _workspace_dirs(monkeypatch, tmp_path)
  first = cw.paths.fresh_workspace_name('ask-ppp-dev')
  second = cw.paths.fresh_workspace_name('ask-ppp-dev')
  assert first.startswith('ask-ppp-dev-')
  assert first != second


def test_fresh_workspace_name_regenerates_on_worktree_collision(monkeypatch, tmp_path):
  worktrees, _ = _workspace_dirs(monkeypatch, tmp_path)
  suffixes = iter(['aaaaaa', 'bbbbbb'])
  monkeypatch.setattr(cw.paths.secrets, 'token_hex', lambda _: next(suffixes))
  (worktrees / 'idea-aaaaaa').mkdir()
  assert cw.paths.fresh_workspace_name('idea') == 'idea-bbbbbb'


def test_fresh_workspace_name_regenerates_on_container_collision(monkeypatch, tmp_path):
  _, containers = _workspace_dirs(monkeypatch, tmp_path)
  suffixes = iter(['aaaaaa', 'bbbbbb'])
  monkeypatch.setattr(cw.paths.secrets, 'token_hex', lambda _: next(suffixes))
  (containers / 'idea-aaaaaa').mkdir()
  assert cw.paths.fresh_workspace_name('idea') == 'idea-bbbbbb'
