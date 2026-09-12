from types import SimpleNamespace
from unittest.mock import MagicMock

import ride.listing as listing
from ride.workspace.metadata import Isolation


def _workspace(name: str, isolation: Isolation, active: bool):
  workspace = MagicMock()
  workspace.name = name
  workspace.isolation = isolation
  workspace.repo = None
  workspace.is_active.return_value = active
  workspace.last_active.return_value = None
  return workspace


def test_badges_describe_isolation_and_liveness(monkeypatch, capsys):
  workspaces = [
    _workspace('boxed-live', Isolation.BOXED, True),
    _workspace('unboxed-live', Isolation.UNBOXED, True),
    _workspace('boxed-idle', Isolation.BOXED, False),
    _workspace('unboxed-idle', Isolation.UNBOXED, False),
  ]
  monkeypatch.setattr(listing.Workspace, 'all', lambda: workspaces)
  monkeypatch.setattr(listing, 'running_mounts', lambda: set())
  monkeypatch.setattr(
    listing,
    'harness_for_workspace',
    lambda workspace: SimpleNamespace(read_subject=lambda value: None),
  )

  assert listing.list_workspaces() == 0

  badges = {line.split()[1]: line.split()[0] for line in capsys.readouterr().out.splitlines()}
  assert badges == {
    'boxed-live': '[o]',
    'unboxed-live': '.o.',
    'boxed-idle': '[-]',
    'unboxed-idle': '.-.',
  }
