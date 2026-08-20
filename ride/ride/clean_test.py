from types import SimpleNamespace
from unittest.mock import MagicMock

import ride.clean as clean
from ride.workspace.metadata import WorkspaceKind


def _workspace(name: str, repo: str, *, is_clean: bool):
  workspace = MagicMock()
  workspace.name = name
  workspace.kind = WorkspaceKind.CONTAINER
  workspace.metadata = SimpleNamespace(repo=repo)
  workspace.is_active.return_value = False
  workspace.is_clean.return_value = (is_clean, [] if is_clean else ['dirty'])
  return workspace


def test_clean_collects_all_global_stores_after_removing_workspaces(monkeypatch):
  removed = _workspace('removed', 'https://example.test/removed.git', is_clean=True)
  retained = _workspace('retained', 'https://example.test/retained.git', is_clean=False)
  monkeypatch.setattr(
    clean.Workspace, 'all', MagicMock(side_effect=[[removed, retained], [retained]])
  )
  monkeypatch.setattr(clean, 'running_mounts', lambda: set())
  mirrors = MagicMock(return_value=(1, 1))
  bundles = MagicMock(return_value=(2, 0))
  monkeypatch.setattr(clean, 'clean_managed_mirrors', mirrors)
  monkeypatch.setattr(clean, 'clean_runtime_bundles', bundles)

  assert clean.clean_workspaces() == 0

  removed.remove.assert_called_once_with(force=False)
  retained.remove.assert_not_called()
  mirrors.assert_called_once_with({'https://example.test/retained.git'}, dry_run=False)
  bundles.assert_called_once_with(dry_run=False)
