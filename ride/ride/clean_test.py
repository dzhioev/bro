import contextlib
import shutil
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

import ride.clean as clean
from ride.attachments import Attachment
from ride.harness import SessionHarness
from ride.repo_attachment import repo as repo_type
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


def _workspace(name: str, repo: str, *, is_clean: bool):
  workspace = MagicMock(spec=Workspace)
  workspace.name = name
  workspace.isolation = Isolation.BOXED
  workspace.attachments = (Attachment('repo', 'repository', {'repo': repo, 'branch': 'branch'}),)
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
  monkeypatch.setattr(clean, 'hold_workspace_removal', lambda *_args: contextlib.nullcontext())
  mirrors = MagicMock(return_value=(1, 1))
  bundles = MagicMock(return_value=(2, 0))
  controls = MagicMock(return_value=(1, 1))
  harness = MagicMock(spec=SessionHarness)
  monkeypatch.setattr(repo_type, 'clean_shared', mirrors)
  monkeypatch.setattr(clean, 'clean_runtime_bundles', bundles)
  monkeypatch.setattr(clean, 'clean_controls', controls)
  monkeypatch.setattr(clean, 'installed_harness_names', lambda: ('example',))
  monkeypatch.setattr(clean, 'get_harness', lambda _name: harness)

  assert clean.clean_workspaces() == 0

  removed.remove.assert_called_once_with(force=False)
  retained.remove.assert_not_called()
  mirrors.assert_called_once_with(list(retained.attachments), dry_run=False)
  bundles.assert_called_once_with(dry_run=False)
  controls.assert_called_once_with(dry_run=False)
  harness.clean_cache.assert_called_once_with(dry_run=False)


def test_clean_aborts_before_removing_anything_when_docker_is_unreachable(monkeypatch):
  workspace = _workspace('ws', 'https://example.test/ws.git', is_clean=True)
  monkeypatch.setattr(clean.Workspace, 'all', MagicMock(return_value=[workspace]))

  def unreachable():
    raise RuntimeError('docker ps failed: cannot connect')

  monkeypatch.setattr(clean, 'running_mounts', unreachable)

  assert clean.clean_workspaces() == 1

  workspace.remove.assert_not_called()


def test_dry_run_does_not_create_a_workspace_lock(monkeypatch):
  workspace = clean.Workspace.create('dry', (), clean.Isolation.UNBOXED)
  monkeypatch.setattr(repo_type, 'clean_shared', MagicMock())
  monkeypatch.setattr(clean, 'clean_runtime_bundles', MagicMock(return_value=(0, 0)))
  monkeypatch.setattr(clean, 'installed_harness_names', lambda: ())

  assert clean.clean_workspaces(dry_run=True, names=['dry']) == 0
  assert not workspace.lockfile.exists()


def test_force_removes_an_explicit_unrecognized_workspace_without_reading_it(monkeypatch, tmp_path):
  path = tmp_path / 'workspaces' / 'old'
  path.mkdir(parents=True)
  (path / 'unknown').write_text('{}')
  monkeypatch.setattr(
    clean.Workspace,
    'open',
    MagicMock(side_effect=clean.UnrecognizedWorkspaceRecord('unrecognized')),
  )
  monkeypatch.setattr(clean, 'workspace_dir', lambda _name: path)
  monkeypatch.setattr(clean, 'running_mounts', lambda: set())
  monkeypatch.setattr(clean, 'hold_workspace_removal', lambda *_args: contextlib.nullcontext())
  removed = MagicMock(side_effect=shutil.rmtree)
  monkeypatch.setattr(clean, 'remove_workspace_path', removed)
  monkeypatch.setattr(clean.Workspace, 'all', MagicMock(return_value=[]))
  monkeypatch.setattr(repo_type, 'clean_shared', MagicMock())
  monkeypatch.setattr(clean, 'clean_runtime_bundles', MagicMock(return_value=(0, 0)))
  monkeypatch.setattr(clean, 'installed_harness_names', lambda: ())

  assert clean.clean_workspaces(force=True, names=['old']) == 0
  removed.assert_called_once_with(path)


@pytest.mark.parametrize('names', [('first',), ('second',), ('first', 'second')])
@pytest.mark.parametrize('dry_run', [False, True])
def test_clean_dispatches_to_each_installed_harness(monkeypatch, names, dry_run):
  harnesses = {name: MagicMock(spec=SessionHarness) for name in names}
  resolver = MagicMock(side_effect=harnesses.__getitem__)
  monkeypatch.setattr(clean.Workspace, 'all', list)
  monkeypatch.setattr(repo_type, 'clean_shared', MagicMock())
  monkeypatch.setattr(clean, 'clean_runtime_bundles', MagicMock(return_value=(0, 0)))
  monkeypatch.setattr(clean, 'installed_harness_names', lambda: names)
  monkeypatch.setattr(clean, 'get_harness', resolver)

  assert clean.clean_workspaces(dry_run=dry_run) == 0
  assert [call.args[0] for call in resolver.call_args_list] == list(names)
  for harness in harnesses.values():
    harness.clean_cache.assert_called_once_with(dry_run=dry_run)


def test_importing_clean_loads_no_harness():
  probe = (
    'import sys; import ride.clean; '
    'assert "bro.claude.claude_release" not in sys.modules; '
    'assert "bro.claude.harness" not in sys.modules; '
    'assert "bro.native.harness" not in sys.modules'
  )
  subprocess.run([sys.executable, '-c', probe], check=True, capture_output=True, text=True)


@pytest.mark.parametrize(
  'record',
  [
    '{',
    '[]',
    '{}',
    '{"isolation":"boxed","repo":"/repo","branch":"work","throwaway":false,"tree":null}',
    '{"isolation":"boxed","attachments":[{"type":"uninstalled","name":"data"}],"throwaway":false,"tree":null}',
    None,
    False,
  ],
)
def test_unreadable_records_are_named_listed_retained_and_force_removed(
  record,
  monkeypatch,
  tmp_path,
  capsys,
  caplog,
):
  import ride.listing as listing
  import ride.workspace.model as model
  from bro.workspace.paths import workspace_dir
  from ride.repository import repositories_dir
  from ride.workspace.model import UnrecognisedWorkspace

  path = workspace_dir('unreadable')
  path.mkdir(parents=True)
  if record is None:
    (path / 'workspace.json').mkdir()
  elif record is not False:
    (path / 'workspace.json').write_text(record)
  mirror = repositories_dir() / 'retained-mirror'
  mirror.mkdir(parents=True)
  monkeypatch.setattr(clean, 'running_mounts', lambda: set())
  monkeypatch.setattr(clean, 'clean_controls', lambda **kwargs: (0, 0))
  monkeypatch.setattr(clean, 'clean_runtime_bundles', lambda **kwargs: (0, 0))
  monkeypatch.setattr(clean, 'installed_harness_names', lambda: ())
  monkeypatch.setattr(model, '_cleanup_image', lambda: None)

  with pytest.raises(
    clean.UnrecognizedWorkspaceRecord, match='ride clean --force unreadable'
  ) as caught:
    Workspace.open('unreadable')
  records = Workspace.all()
  assert len(records) == 1 and isinstance(records[0], UnrecognisedWorkspace)
  assert listing.list_workspaces() == 0
  assert 'unrecognised' in capsys.readouterr().out
  assert str(caught.value) in caplog.text
  external = tmp_path / 'external'
  external.mkdir()
  owner = Workspace.create('owner', (), Isolation.UNBOXED, tree=external)
  assert owner.tree == external
  with pytest.raises(ValueError, match='already recorded'):
    Workspace.create('other', (), Isolation.UNBOXED, tree=external)
  assert clean.clean_workspaces() == 0
  assert path.is_dir() and mirror.is_dir()
  with pytest.raises(clean.UnrecognizedWorkspaceRecord, match='ride clean --force unreadable'):
    clean.clean_workspaces(names=['unreadable'])
  assert clean.clean_workspaces(force=True, names=['unreadable']) == 0
  assert not path.exists()
