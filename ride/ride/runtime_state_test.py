import contextlib
import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from bro.workspace.paths import runtime_base
from ride import runtime_state
from ride.runtime_state import RuntimeStateMigrationError, migrate_legacy_runtime_state


def _legacy_root(base: Path, name: str = 'project-1234abcd') -> Path:
  root = base / name
  root.mkdir(parents=True)
  return root


def _workspace(root: Path, name: str, *, kind: str = 'container') -> Path:
  workspace = root / 'workspaces' / name
  workspace.mkdir(parents=True)
  (workspace / 'meta.json').write_text(
    json.dumps({'kind': kind, 'branch': f'worktree-{name}', 'throwaway': False})
  )
  (workspace / 'resume.json').write_text(json.dumps({'name': name, 'resume': True}))
  return workspace


def _git(*arguments: str, cwd: Path) -> None:
  subprocess.run(['git', *arguments], cwd=cwd, check=True, capture_output=True)


def test_clean_migration_merges_stores_and_backfills_clone_attachment(monkeypatch, caplog):
  base = runtime_base()
  root = _legacy_root(base)
  workspace = _workspace(root, 'session')
  tree = workspace / 'tree'
  tree.mkdir()
  _git('init', '--quiet', cwd=tree)
  _git('remote', 'add', 'origin', 'HTTPS://Example.Test/owner/repo.git/', cwd=tree)
  alternates = tree / '.git' / 'objects' / 'info' / 'alternates'
  alternates.parent.mkdir(parents=True, exist_ok=True)
  alternates.write_text('/host-repo/.git/objects\n')
  (root / 'trails' / 'trails' / 'trail-id').mkdir(parents=True)
  (root / 'trails' / 'trails' / 'trail-id' / 'header.json').write_text('{}')
  (root / 'summon').mkdir()
  (root / 'summon' / 'session.jsonl').write_text('{"event":"sent","request_id":"request-id"}\n')
  (root / 'broker').mkdir()
  (root / 'broker' / 'request.sock').write_text('stale')
  monkeypatch.setattr(runtime_state, '_running_mounts', lambda: set())

  with caplog.at_level('INFO'):
    migrate_legacy_runtime_state()

  assert not root.exists()
  migrated = base / 'workspaces' / 'session'
  assert json.loads((migrated / 'meta.json').read_text()) == {
    'kind': 'container',
    'throwaway': False,
    'repo': 'https://example.test/owner/repo.git',
    'branch': 'worktree-session',
  }
  assert json.loads((migrated / 'resume.json').read_text())['repo'] == (
    'https://example.test/owner/repo.git'
  )
  assert (migrated / 'tree' / '.git' / 'objects' / 'info' / 'alternates').read_text() == (
    '/host-repo/objects\n'
  )
  assert (base / 'trails' / 'trails' / 'trail-id' / 'header.json').is_file()
  assert (base / 'summon' / 'session.jsonl').is_file()
  assert (base / 'broker' / 'request.sock').is_file()
  assert '1 workspace(s)' in caplog.text


def test_worktree_attachment_and_registration_follow_the_moved_workspace():
  base = runtime_base()
  repository = base.parent / 'repository'
  repository.mkdir(parents=True)
  _git('init', '--quiet', cwd=repository)
  _git('config', 'user.name', 'Test', cwd=repository)
  _git('config', 'user.email', 'test@example.test', cwd=repository)
  _git('commit', '--allow-empty', '--quiet', '-m', 'initial', cwd=repository)
  root = _legacy_root(base)
  workspace = _workspace(root, 'session', kind='worktree')
  _git(
    'worktree', 'add', '--quiet', '-b', 'worktree-session', str(workspace / 'tree'), cwd=repository
  )

  migrate_legacy_runtime_state()

  migrated = base / 'workspaces' / 'session'
  metadata = json.loads((migrated / 'meta.json').read_text())
  assert metadata['repo'] == str(repository)
  assert json.loads((migrated / 'resume.json').read_text())['repo'] == str(repository)
  worktree_listing = subprocess.run(
    ['git', 'worktree', 'list', '--porcelain'],
    cwd=repository,
    check=True,
    capture_output=True,
    text=True,
  ).stdout
  assert str(migrated / 'tree') in worktree_listing
  assert str(workspace / 'tree') not in worktree_listing


def test_partial_rerun_repairs_a_worktree_moved_before_cleanup():
  base = runtime_base()
  repository = base.parent / 'repository'
  repository.mkdir(parents=True)
  _git('init', '--quiet', cwd=repository)
  _git('config', 'user.name', 'Test', cwd=repository)
  _git('config', 'user.email', 'test@example.test', cwd=repository)
  _git('commit', '--allow-empty', '--quiet', '-m', 'initial', cwd=repository)
  root = _legacy_root(base)
  source = _workspace(root, 'session', kind='worktree')
  _git('worktree', 'add', '--quiet', '-b', 'worktree-session', str(source / 'tree'), cwd=repository)
  destination = base / 'workspaces' / 'session'
  metadata = {
    'kind': 'worktree',
    'throwaway': False,
    'repo': str(repository),
    'branch': 'worktree-session',
  }
  (source / 'meta.json').write_text(json.dumps(metadata))
  (source / 'resume.json').write_text(json.dumps({'name': 'session', 'repo': str(repository)}))
  pending = base / 'runtime' / runtime_state._PENDING_WORKTREES
  pending.parent.mkdir(parents=True)
  pending.write_text(
    json.dumps(
      {
        'repairs': [
          {'source': str(source), 'destination': str(destination), 'repo': str(repository)}
        ]
      }
    )
  )
  destination.parent.mkdir(parents=True)
  source.rename(destination)

  migrate_legacy_runtime_state()

  assert not root.exists()
  assert not pending.exists()
  listing = subprocess.run(
    ['git', 'worktree', 'list', '--porcelain'],
    cwd=repository,
    check=True,
    capture_output=True,
    text=True,
  ).stdout
  assert str(destination / 'tree') in listing


def test_workspace_name_collision_refuses_the_whole_migration(monkeypatch):
  base = runtime_base()
  first = _legacy_root(base, 'first-1234abcd')
  second = _legacy_root(base, 'second-5678efab')
  first_workspace = _workspace(first, 'shared', kind='worktree')
  second_workspace = _workspace(second, 'shared', kind='worktree')
  monkeypatch.setattr(runtime_state, '_running_mounts', lambda: set())

  with pytest.raises(RuntimeStateMigrationError) as raised:
    migrate_legacy_runtime_state()

  message = str(raised.value)
  assert str(first_workspace) in message
  assert str(second_workspace) in message
  assert first_workspace.exists()
  assert second_workspace.exists()
  assert not (base / 'workspaces').exists()


def test_collision_in_another_store_does_not_move_preflighted_workspaces(monkeypatch):
  base = runtime_base()
  root = _legacy_root(base)
  workspace = _workspace(root, 'session', kind='worktree')
  (root / 'summon').mkdir()
  source = root / 'summon' / 'session.jsonl'
  source.write_text('old')
  destination = base / 'summon' / 'session.jsonl'
  destination.parent.mkdir()
  destination.write_text('new')
  monkeypatch.setattr(runtime_state, '_running_mounts', lambda: set())

  with pytest.raises(RuntimeStateMigrationError) as raised:
    migrate_legacy_runtime_state()

  assert str(source) in str(raised.value)
  assert str(destination) in str(raised.value)
  assert workspace.exists()
  assert not (base / 'workspaces' / 'session').exists()


def test_live_locked_workspace_aborts_migration(monkeypatch):
  base = runtime_base()
  root = _legacy_root(base)
  workspace = _workspace(root, 'active', kind='worktree')
  lock = (workspace / 'lock').open('w')
  monkeypatch.setattr(runtime_state, '_running_mounts', lambda: set())

  with contextlib.closing(lock):
    fcntl.flock(lock, fcntl.LOCK_EX)
    with pytest.raises(RuntimeStateMigrationError, match="'active' is live"):
      migrate_legacy_runtime_state()

  assert workspace.exists()


def test_running_container_aborts_migration(monkeypatch):
  base = runtime_base()
  root = _legacy_root(base)
  workspace = _workspace(root, 'active')
  tree = workspace / 'tree'
  tree.mkdir()
  monkeypatch.setattr(runtime_state, '_running_mounts', lambda: {str(tree)})

  with pytest.raises(RuntimeStateMigrationError, match='container running'):
    migrate_legacy_runtime_state()

  assert workspace.exists()


def test_partial_rerun_finishes_remaining_state(monkeypatch):
  base = runtime_base()
  root = _legacy_root(base)
  workspace = _workspace(root, 'session', kind='worktree')
  metadata = {'kind': 'worktree', 'throwaway': False}
  (workspace / 'meta.json').write_text(json.dumps(metadata))
  (workspace / 'resume.json').write_text(json.dumps({'name': 'session', 'repo': None}))
  destination_trails = base / 'trails' / 'trails'
  destination_trails.mkdir(parents=True)
  (destination_trails / 'already-moved').mkdir()
  (root / 'trails' / 'trails' / 'remaining').mkdir(parents=True)
  (root / 'trails' / 'trails' / 'remaining' / 'header.json').write_text('{}')
  monkeypatch.setattr(runtime_state, '_running_mounts', lambda: set())

  migrate_legacy_runtime_state()

  assert not root.exists()
  assert (destination_trails / 'already-moved').is_dir()
  assert (destination_trails / 'remaining' / 'header.json').is_file()
  assert json.loads((base / 'workspaces' / 'session' / 'meta.json').read_text()) == metadata


def test_duplicate_trail_ids_refuse_migration_even_when_the_directories_are_empty():
  base = runtime_base()
  first = _legacy_root(base, 'first-1234abcd')
  second = _legacy_root(base, 'second-5678efab')
  first_trail = first / 'trails' / 'trails' / 'same-trail'
  second_trail = second / 'trails' / 'trails' / 'same-trail'
  first_trail.mkdir(parents=True)
  second_trail.mkdir(parents=True)

  with pytest.raises(RuntimeStateMigrationError) as raised:
    migrate_legacy_runtime_state()

  assert str(first_trail) in str(raised.value)
  assert str(second_trail) in str(raised.value)


def test_duplicate_request_ids_across_audit_files_refuse_migration():
  base = runtime_base()
  first = _legacy_root(base, 'first-1234abcd')
  second = _legacy_root(base, 'second-5678efab')
  first_audit = first / 'summon' / 'first.jsonl'
  second_audit = second / 'summon' / 'second.jsonl'
  first_audit.parent.mkdir()
  second_audit.parent.mkdir()
  entry = '{"event":"sent","request_id":"same-request"}\n'
  first_audit.write_text(entry)
  second_audit.write_text(entry)

  with pytest.raises(RuntimeStateMigrationError) as raised:
    migrate_legacy_runtime_state()

  assert str(first_audit) in str(raised.value)
  assert str(second_audit) in str(raised.value)


def test_identical_tool_blobs_from_multiple_roots_are_deduplicated():
  base = runtime_base()
  first = _legacy_root(base, 'first-1234abcd')
  second = _legacy_root(base, 'second-5678efab')
  for root in (first, second):
    tools = root / 'trails' / 'trails' / 'tools'
    tools.mkdir(parents=True)
    (tools / 'abc.json').write_text('{"name":"tool"}')

  migrate_legacy_runtime_state()

  assert not first.exists()
  assert not second.exists()
  assert (base / 'trails' / 'trails' / 'tools' / 'abc.json').read_text() == '{"name":"tool"}'


def test_noop_does_not_create_migration_state():
  base = runtime_base()
  (base / 'workspaces').mkdir(parents=True)

  migrate_legacy_runtime_state()

  assert not (base / 'runtime').exists()
