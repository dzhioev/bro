"""migration of checkout-keyed runtime state into the global flat stores."""

import contextlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from bro.base import log
from bro.workspace.paths import find_project_root, runtime_base
from ride.repository import is_git_url, normalize_git_url
from ride.workspace.metadata import WorkspaceKind, WorkspaceMetadata

_PROJECT_KEY = re.compile(r'^.+-[0-9a-f]{8}$')
_WORKSPACES = 'workspaces'
_MIGRATION_LOCK = '.state-migration.lock'
_PENDING_WORKTREES = '.state-migration-worktrees.json'
_OLD_METADATA_FIELDS = {'kind', 'branch', 'throwaway'}


class RuntimeStateMigrationError(RuntimeError):
  """legacy runtime state cannot be moved safely into the flat root."""


@dataclass(frozen=True)
class _WorkspaceMigration:
  source: Path
  destination: Path
  metadata: dict[str, Any]
  resume: Optional[dict[str, Any]]


@dataclass(frozen=True)
class _MigrationPlan:
  roots: tuple[Path, ...]
  workspaces: tuple[_WorkspaceMigration, ...]
  detached_workspaces: tuple[str, ...]


def _legacy_roots(base: Path) -> tuple[Path, ...]:
  try:
    entries = os.scandir(base)
  except FileNotFoundError:
    return ()
  with contextlib.closing(entries):
    return tuple(
      sorted(
        Path(entry.path)
        for entry in entries
        if _PROJECT_KEY.fullmatch(entry.name) is not None and entry.is_dir(follow_symlinks=False)
      )
    )


@contextlib.contextmanager
def _migration_lock(base: Path):
  lock_directory = base / 'runtime'
  lock_directory.mkdir(parents=True, exist_ok=True)
  handle = os.fdopen(os.open(lock_directory / _MIGRATION_LOCK, os.O_RDWR | os.O_CREAT, 0o600), 'r+')
  with contextlib.closing(handle):
    fcntl.flock(handle, fcntl.LOCK_EX)
    yield


def _read_object(path: Path) -> dict[str, Any]:
  try:
    value = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError) as error:
    raise RuntimeStateMigrationError(
      f'cannot read legacy runtime record {path}: {error}'
    ) from error
  if not isinstance(value, dict):
    raise RuntimeStateMigrationError(f'legacy runtime record must be an object: {path}')
  return value


def _atomic_bytes(path: Path, value: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
  temporary = Path(temporary_name)
  try:
    with os.fdopen(descriptor, 'wb') as stream:
      stream.write(value)
      stream.flush()
      os.fsync(stream.fileno())
    temporary.replace(path)
  except BaseException:
    temporary.unlink(missing_ok=True)
    raise


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
  _atomic_bytes(path, json.dumps(value, indent=2).encode())


def _worktree_attachment(tree: Path) -> Optional[str]:
  root = find_project_root(tree)
  return None if root is None else str(root)


def _container_attachment(tree: Path) -> Optional[str]:
  result = subprocess.run(
    ['git', 'config', '--get', 'remote.origin.url'],
    cwd=tree,
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    return None
  origin = result.stdout.strip()
  if len(origin) == 0:
    return None
  if is_git_url(origin):
    try:
      return normalize_git_url(origin)
    except ValueError:
      return None
  path = Path(origin).expanduser()
  if not path.is_absolute() or not path.exists():
    return None
  root = find_project_root(path)
  return None if root is None else str(root)


def _workspace_attachment(path: Path, kind: WorkspaceKind) -> Optional[str]:
  tree = path / 'tree'
  if not tree.is_dir():
    return None
  if kind is WorkspaceKind.WORKTREE:
    attachment = _worktree_attachment(tree)
  else:
    attachment = _container_attachment(tree)
  if attachment is None and any(tree.iterdir()):
    raise RuntimeStateMigrationError(
      f'cannot recover the repository attachment for materialized legacy workspace {path}'
    )
  return attachment


def _workspace_migration(source: Path, destination: Path) -> tuple[_WorkspaceMigration, bool]:
  metadata_path = source / 'meta.json'
  metadata = _read_object(metadata_path)
  if metadata.keys() == _OLD_METADATA_FIELDS:
    try:
      kind = WorkspaceKind(metadata['kind'])
    except (KeyError, ValueError) as error:
      raise RuntimeStateMigrationError(
        f'invalid legacy workspace metadata: {metadata_path}'
      ) from error
    if not isinstance(metadata['branch'], str) or len(metadata['branch']) == 0:
      raise RuntimeStateMigrationError(
        f'legacy workspace branch must be a non-empty string: {metadata_path}'
      )
    if not isinstance(metadata['throwaway'], bool):
      raise RuntimeStateMigrationError(
        f'legacy workspace throwaway must be a bool: {metadata_path}'
      )
    attachment = _workspace_attachment(source, kind)
    migrated = WorkspaceMetadata(
      kind=kind,
      repo=attachment,
      branch=metadata['branch'] if attachment is not None else None,
      throwaway=metadata['throwaway'],
    ).dump()
  else:
    try:
      current = WorkspaceMetadata.load(metadata)
    except (TypeError, ValueError) as error:
      raise RuntimeStateMigrationError(
        f'invalid workspace metadata during migration: {metadata_path}'
      ) from error
    attachment = current.repo
    migrated = current.dump()

  resume_path = source / 'resume.json'
  resume = _read_object(resume_path) if resume_path.is_file() else None
  if resume is not None:
    if 'repo' in resume and resume['repo'] != attachment:
      raise RuntimeStateMigrationError(
        f'workspace attachment disagrees between {metadata_path} and {resume_path}'
      )
    resume = {**resume, 'repo': attachment}
  return _WorkspaceMigration(source, destination, migrated, resume), attachment is None


def _legacy_workspaces(roots: tuple[Path, ...]) -> tuple[Path, ...]:
  workspaces = []
  for root in roots:
    store = root / _WORKSPACES
    if not store.exists():
      continue
    if not store.is_dir():
      raise RuntimeStateMigrationError(f'legacy workspace store is not a directory: {store}')
    for workspace in sorted(store.iterdir()):
      if not workspace.is_dir():
        raise RuntimeStateMigrationError(f'legacy workspace entry is not a directory: {workspace}')
      workspaces.append(workspace)
  return tuple(workspaces)


@contextlib.contextmanager
def _hold_workspace_locks(workspaces: tuple[Path, ...]):
  with contextlib.ExitStack() as stack:
    for workspace in workspaces:
      lock = workspace / 'lock'
      handle = os.fdopen(os.open(lock, os.O_RDWR | os.O_CREAT, 0o644), 'r+')
      stack.enter_context(contextlib.closing(handle))
      try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError as error:
        raise RuntimeStateMigrationError(
          f'legacy workspace {workspace.name!r} is live (session lock held): {workspace}'
        ) from error
    yield


def _running_mounts() -> set[str]:
  try:
    ids = subprocess.run(['docker', 'ps', '-q'], capture_output=True, text=True)
  except FileNotFoundError as error:
    raise RuntimeStateMigrationError(
      'cannot verify legacy container workspaces because the docker command is unavailable'
    ) from error
  if ids.returncode != 0:
    raise RuntimeStateMigrationError(
      f'cannot verify legacy container workspaces: docker ps failed: {ids.stderr.strip()}'
    )
  container_ids = ids.stdout.split()
  if len(container_ids) == 0:
    return set()
  inspect = subprocess.run(
    ['docker', 'inspect', '--format', '{{range .Mounts}}{{.Source}}\n{{end}}', *container_ids],
    capture_output=True,
    text=True,
  )
  if inspect.returncode != 0:
    raise RuntimeStateMigrationError(
      f'cannot verify legacy container workspaces: docker inspect failed: {inspect.stderr.strip()}'
    )
  return {line for line in inspect.stdout.splitlines() if len(line) > 0}


def _path_exists(path: Path) -> bool:
  return os.path.lexists(path)


def _collision(source: Path, other: Path) -> RuntimeStateMigrationError:
  return RuntimeStateMigrationError(f'legacy runtime state collision between {source} and {other}')


def _is_tool_blob(path: Path, base: Path) -> bool:
  try:
    relative = path.relative_to(base)
  except ValueError:
    return False
  return relative.parts[:3] == ('trails', 'trails', 'tools') and path.suffix == '.json'


def _identical_tool_blobs(source: Path, other: Path, destination: Path, base: Path) -> bool:
  return (
    _is_tool_blob(destination, base)
    and source.is_file()
    and other.is_file()
    and source.read_bytes() == other.read_bytes()
  )


def _preflight_store(
  source: Path,
  destination: Path,
  base: Path,
  planned_files: dict[Path, Path],
) -> None:
  if source.is_dir() and not source.is_symlink():
    if _path_exists(destination) and (not destination.is_dir() or destination.is_symlink()):
      raise _collision(source, destination)
    for child in source.iterdir():
      _preflight_store(child, destination / child.name, base, planned_files)
    return

  other = planned_files.get(destination)
  if other is not None:
    if not _identical_tool_blobs(source, other, destination, base):
      raise _collision(source, other)
    return
  if _path_exists(destination):
    if not _identical_tool_blobs(source, destination, destination, base):
      raise _collision(source, destination)
    return
  planned_files[destination] = source


def _preflight_trail_ids(base: Path, roots: tuple[Path, ...]) -> None:
  trail_sources: dict[str, Path] = {}
  stores = [base / 'trails' / 'trails', *(root / 'trails' / 'trails' for root in roots)]
  for store in stores:
    if not store.is_dir():
      continue
    for trail in sorted(store.iterdir()):
      if trail.name == 'tools' or not trail.is_dir():
        continue
      other = trail_sources.get(trail.name)
      if other is not None and other != trail:
        raise _collision(trail, other)
      trail_sources[trail.name] = trail


def _audit_request_ids(path: Path) -> set[str]:
  request_ids = set()
  try:
    lines = path.read_text().splitlines()
  except OSError as error:
    raise RuntimeStateMigrationError(f'cannot read summon audit {path}: {error}') from error
  for index, line in enumerate(lines, start=1):
    try:
      entry = json.loads(line)
    except json.JSONDecodeError as error:
      raise RuntimeStateMigrationError(
        f'invalid summon audit line {path}:{index}: {error}'
      ) from error
    request_id = entry.get('request_id') if isinstance(entry, dict) else None
    if not isinstance(request_id, str) or len(request_id) == 0:
      raise RuntimeStateMigrationError(f'summon audit line has no request id: {path}:{index}')
    request_ids.add(request_id)
  return request_ids


def _preflight_summon_request_ids(base: Path, roots: tuple[Path, ...]) -> None:
  request_sources: dict[str, Path] = {}
  stores = [base / 'summon', *(root / 'summon' for root in roots)]
  for store in stores:
    if not store.is_dir():
      continue
    for audit in sorted(store.glob('*.jsonl')):
      for request_id in _audit_request_ids(audit):
        other = request_sources.get(request_id)
        if other is not None and other != audit:
          raise _collision(audit, other)
        request_sources[request_id] = audit


def _build_plan(
  base: Path, roots: tuple[Path, ...], workspace_sources: tuple[Path, ...]
) -> _MigrationPlan:
  workspace_names: dict[str, Path] = {}
  workspaces: list[_WorkspaceMigration] = []
  detached: list[str] = []
  container_trees: dict[str, Path] = {}

  for source in workspace_sources:
    destination = base / _WORKSPACES / source.name
    other = workspace_names.get(source.name)
    if other is not None:
      raise _collision(source, other)
    if _path_exists(destination):
      raise _collision(source, destination)
    migration, is_detached = _workspace_migration(source, destination)
    workspace_names[source.name] = source
    workspaces.append(migration)
    if is_detached:
      detached.append(source.name)
    tree = source / 'tree'
    if migration.metadata['kind'] == WorkspaceKind.CONTAINER.value and tree.is_dir():
      container_trees[source.name] = tree

  if len(container_trees) > 0:
    mounts = _running_mounts()
    for name, tree in container_trees.items():
      if str(tree) in mounts:
        raise RuntimeStateMigrationError(
          f'legacy workspace {name!r} is live (container running): {tree}'
        )

  planned_files: dict[Path, Path] = {}
  for root in roots:
    for store in root.iterdir():
      if store.name != _WORKSPACES:
        _preflight_store(store, base / store.name, base, planned_files)
  _preflight_trail_ids(base, roots)
  _preflight_summon_request_ids(base, roots)
  return _MigrationPlan(tuple(roots), tuple(workspaces), tuple(detached))


def _merge_store(source: Path, destination: Path, base: Path) -> None:
  if not _path_exists(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return
  if source.is_dir() and not source.is_symlink():
    for child in list(source.iterdir()):
      _merge_store(child, destination / child.name, base)
    source.rmdir()
    return
  if _identical_tool_blobs(source, destination, destination, base):
    source.unlink()
    return
  raise _collision(source, destination)


def _pending_worktrees_file(base: Path) -> Path:
  return base / 'runtime' / _PENDING_WORKTREES


def _worktree_repairs(plan: _MigrationPlan) -> list[dict[str, str]]:
  repairs = []
  for workspace in plan.workspaces:
    if workspace.metadata['kind'] != WorkspaceKind.WORKTREE.value:
      continue
    repo = workspace.metadata.get('repo')
    if not isinstance(repo, str) or not (workspace.source / 'tree').is_dir():
      continue
    repairs.append(
      {'source': str(workspace.source), 'destination': str(workspace.destination), 'repo': repo}
    )
  return repairs


def _repair_pending_worktrees(base: Path) -> None:
  pending_file = _pending_worktrees_file(base)
  if not pending_file.is_file():
    return
  data = _read_object(pending_file)
  records = data.get('repairs')
  if not isinstance(records, list):
    raise RuntimeStateMigrationError(f'invalid pending worktree repairs: {pending_file}')
  remaining = []
  for record in records:
    if not isinstance(record, dict) or record.keys() != {'source', 'destination', 'repo'}:
      raise RuntimeStateMigrationError(f'invalid pending worktree repair in {pending_file}')
    if not all(isinstance(record[key], str) for key in record):
      raise RuntimeStateMigrationError(f'invalid pending worktree repair in {pending_file}')
    source = Path(record['source'])
    destination = Path(record['destination'])
    if (destination / 'tree').is_dir():
      result = subprocess.run(
        ['git', 'worktree', 'repair', str(destination / 'tree')],
        cwd=record['repo'],
        capture_output=True,
        text=True,
      )
      if result.returncode != 0:
        raise RuntimeStateMigrationError(
          f'cannot repair migrated worktree {destination}: {result.stderr.strip()}'
        )
    elif (source / 'tree').is_dir():
      remaining.append(record)
    else:
      raise RuntimeStateMigrationError(
        f'pending migrated worktree exists at neither {source} nor {destination}'
      )
  if len(remaining) == 0:
    pending_file.unlink()
  else:
    _atomic_json(pending_file, {'repairs': remaining})


def _prepare_container_alternates(workspace: _WorkspaceMigration) -> None:
  repo = workspace.metadata.get('repo')
  if not isinstance(repo, str) or not is_git_url(repo):
    return
  alternates = workspace.source / 'tree' / '.git' / 'objects' / 'info' / 'alternates'
  if alternates.is_file():
    _atomic_bytes(alternates, b'/host-repo/objects\n')


def _apply_plan(base: Path, plan: _MigrationPlan) -> None:
  repairs = _worktree_repairs(plan)
  if len(repairs) > 0:
    _atomic_json(_pending_worktrees_file(base), {'repairs': repairs})
  for workspace in plan.workspaces:
    _atomic_json(workspace.source / 'meta.json', workspace.metadata)
    if workspace.resume is not None:
      _atomic_json(workspace.source / 'resume.json', workspace.resume)
    _prepare_container_alternates(workspace)
    workspace.destination.parent.mkdir(parents=True, exist_ok=True)
    workspace.source.rename(workspace.destination)
  _repair_pending_worktrees(base)

  for root in plan.roots:
    workspace_store = root / _WORKSPACES
    if workspace_store.is_dir():
      workspace_store.rmdir()
    for store in list(root.iterdir()):
      _merge_store(store, base / store.name, base)
    root.rmdir()


def migrate_legacy_runtime_state() -> None:
  """move every historical project-key root into the flat global runtime root."""
  base = runtime_base()
  roots = _legacy_roots(base)
  if len(roots) == 0 and not _pending_worktrees_file(base).is_file():
    return
  with _migration_lock(base):
    _repair_pending_worktrees(base)
    roots = _legacy_roots(base)
    if len(roots) == 0:
      return
    workspace_sources = _legacy_workspaces(roots)
    with _hold_workspace_locks(workspace_sources):
      plan = _build_plan(base, roots, workspace_sources)
      _apply_plan(base, plan)
  log.info(
    'migrated legacy runtime state from %d project root(s): %d workspace(s)',
    len(plan.roots),
    len(plan.workspaces),
  )
  for root in plan.roots:
    log.info('migrated %s into %s', root, base)
  if len(plan.detached_workspaces) > 0:
    log.warning(
      'migrated workspace(s) without a recoverable repository attachment as detached: %s',
      ', '.join(plan.detached_workspaces),
    )
