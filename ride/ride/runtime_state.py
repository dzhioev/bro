"""migration of checkout-keyed runtime state into the global flat stores."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any, Optional

from bro.base import log
from bro.base.git_url import is_git_url, normalize_git_url
from bro.workspace.paths import find_project_root, runtime_base
from ride.workspace.docker import running_mounts
from ride.workspace.metadata import Isolation, WorkspaceMetadata

_PROJECT_KEY = re.compile(r'^.+-[0-9a-f]{8}$')
_PROJECT_KEY_BYTES = 4
_UNSAFE_IN_KEY = re.compile(r'[^A-Za-z0-9._-]')
_WORKSPACES = 'workspaces'
_MIGRATION_LOCK = '.state-migration.lock'
_PENDING_WORKTREES = '.state-migration-worktrees.json'


class RuntimeStateMigrationError(RuntimeError):
  """legacy runtime state cannot be moved safely into the flat root."""


@dataclass(frozen=True)
class _WorkspaceMigration:
  source: Path
  destination: Path
  metadata: dict[str, Any]
  resume: Optional[dict[str, Any]]
  # the attachment came off a container clone's `origin` rather than the root's
  # own checkout, so it names the repository but not the identity the workspace
  # was launched under
  url_recovered: bool


@dataclass(frozen=True)
class _MigrationPlan:
  roots: tuple[Path, ...]
  workspaces: tuple[_WorkspaceMigration, ...]
  detached_workspaces: tuple[str, ...]
  urls_recovered: tuple[str, ...]


def _project_key(project: Path) -> str:
  """the legacy root name a checkout's runtime state lived under: the checkout's
  own directory name plus a digest of its canonical path. One-way, so it reads a
  candidate path rather than yielding one."""
  canonical = str(project.resolve())
  digest = hashlib.blake2b(canonical.encode(), digest_size=_PROJECT_KEY_BYTES).hexdigest()
  return f'{_UNSAFE_IN_KEY.sub("-", Path(canonical).name)}-{digest}'


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


def _root_checkout(root: Path, workspaces: tuple[Path, ...]) -> Optional[str]:
  """the checkout a legacy project-key root holds the state of.

  Every workspace under one is attached to it: the key derives from the checkout
  path, and these roots predate both detached sessions and URL attachments. Only
  a legacy unboxed workspace still names the path — its tree is a linked worktree
  of the checkout, while a legacy boxed workspace's is a clone whose `origin` the
  entrypoint retargeted to the upstream URL. `_project_key` confirms a candidate
  is the checkout this root is keyed on."""
  for workspace in workspaces:
    tree = workspace / 'tree'
    if not tree.is_dir():
      continue
    candidate = find_project_root(tree)
    if candidate is not None and _project_key(candidate) == root.name:
      return str(candidate)
  return None


def _workspace_attachment(
  path: Path, isolation: Isolation, checkout: Optional[str]
) -> tuple[Optional[str], bool]:
  """the attachment to record for a legacy workspace, and whether it fell back to
  the URL its clone's `origin` names (`_WorkspaceMigration.url_recovered`)."""
  tree = path / 'tree'
  if not tree.is_dir():
    return None, False
  if checkout is not None:
    return checkout, False
  if isolation is Isolation.UNBOXED:
    attachment = _worktree_attachment(tree)
  else:
    attachment = _container_attachment(tree)
  if attachment is None and any(tree.iterdir()):
    raise RuntimeStateMigrationError(
      f'cannot recover the repository attachment for materialized legacy workspace {path}'
    )
  return attachment, attachment is not None and is_git_url(attachment)


def _isolation_from_kind(value: Any, path: Path) -> Isolation:
  translated = {'container': Isolation.BOXED, 'worktree': Isolation.UNBOXED}
  try:
    return translated[value]
  except (KeyError, TypeError) as error:
    raise RuntimeStateMigrationError(f'invalid legacy workspace kind: {path}') from error


def _migrate_resume(
  resume: dict[str, Any],
  path: Path,
  attachment: Optional[str],
  isolation: Isolation,
) -> dict[str, Any]:
  if 'repo' in resume and resume['repo'] != attachment:
    raise RuntimeStateMigrationError(
      f'workspace attachment disagrees between {path.parent / "meta.json"} and {path}'
    )
  if 'host' in resume:
    if not isinstance(resume['host'], bool):
      raise RuntimeStateMigrationError(f'legacy resume host must be a bool: {path}')
    resume_isolation = Isolation.UNBOXED if resume['host'] else Isolation.BOXED
    if resume_isolation is not isolation:
      raise RuntimeStateMigrationError(
        f'workspace isolation disagrees between {path.parent / "meta.json"} and {path}'
      )
    migrated = {key: value for key, value in resume.items() if key != 'host'}
    migrated.update(
      repo=attachment,
      isolation=isolation.value,
      tree=None,
      runtime_bundle=None,
    )
    return migrated
  if (
    resume.get('isolation') == isolation.value
    and resume.get('tree') is None
    and resume.get('runtime_bundle') is None
  ):
    return resume
  raise RuntimeStateMigrationError(f'invalid workspace resume record during migration: {path}')


def _workspace_migration(
  source: Path,
  destination: Path,
  checkout: Optional[str],
  *,
  recover_attachment: bool = True,
) -> _WorkspaceMigration:
  metadata_path = source / 'meta.json'
  workspace_path = source / 'workspace.json'
  if workspace_path.is_file():
    try:
      current = WorkspaceMetadata.load(_read_object(workspace_path))
    except (TypeError, ValueError) as error:
      raise RuntimeStateMigrationError(f'invalid workspace record: {workspace_path}') from error
    resume_path = source / 'resume.json'
    resume = _read_object(resume_path) if resume_path.is_file() else None
    if resume is not None:
      resume = _migrate_resume(resume, resume_path, current.repo, current.isolation)
    return _WorkspaceMigration(source, destination, current.dump(), resume, False)
  metadata = _read_object(metadata_path)
  allowed = {'kind', 'throwaway', 'repo', 'branch'}
  if not {'kind', 'throwaway'} <= metadata.keys() or not metadata.keys() <= allowed:
    raise RuntimeStateMigrationError(f'invalid legacy workspace metadata: {metadata_path}')
  isolation = _isolation_from_kind(metadata['kind'], metadata_path)
  if not isinstance(metadata['throwaway'], bool):
    raise RuntimeStateMigrationError(f'legacy workspace throwaway must be a bool: {metadata_path}')
  recorded_repo = metadata.get('repo')
  recorded_branch = metadata.get('branch')
  if not recover_attachment and (recorded_repo is None) != (recorded_branch is None):
    raise RuntimeStateMigrationError(
      f'legacy workspace repo and branch must both be present or absent: {metadata_path}'
    )
  if recorded_repo is not None:
    if not isinstance(recorded_repo, str) or recorded_repo == '':
      raise RuntimeStateMigrationError(f'invalid workspace repository: {metadata_path}')
    attachment = recorded_repo
    url_recovered = False
  elif recover_attachment:
    attachment, url_recovered = _workspace_attachment(source, isolation, checkout)
  else:
    attachment = None
    url_recovered = False
  if attachment is None:
    branch = None
  else:
    branch = recorded_branch
    if not isinstance(branch, str) or branch == '':
      raise RuntimeStateMigrationError(
        f'legacy workspace branch must be a non-empty string: {metadata_path}'
      )
  migrated = WorkspaceMetadata(
    isolation=isolation,
    repo=attachment,
    branch=branch,
    throwaway=metadata['throwaway'],
    tree=None,
  ).dump()

  resume_path = source / 'resume.json'
  resume = _read_object(resume_path) if resume_path.is_file() else None
  if resume is not None:
    resume = _migrate_resume(resume, resume_path, attachment, isolation)
  return _WorkspaceMigration(source, destination, migrated, resume, url_recovered)


def _legacy_workspaces(roots: tuple[Path, ...]) -> dict[Path, tuple[Path, ...]]:
  by_root: dict[Path, tuple[Path, ...]] = {}
  for root in roots:
    store = root / _WORKSPACES
    if not store.exists():
      continue
    if not store.is_dir():
      raise RuntimeStateMigrationError(f'legacy workspace store is not a directory: {store}')
    workspaces = []
    for workspace in sorted(store.iterdir()):
      if not workspace.is_dir():
        raise RuntimeStateMigrationError(f'legacy workspace entry is not a directory: {workspace}')
      workspaces.append(workspace)
    by_root[root] = tuple(workspaces)
  return by_root


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
    return running_mounts()
  except (OSError, RuntimeError) as error:
    raise RuntimeStateMigrationError(f'cannot verify legacy boxed workspaces: {error}') from error


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
  base: Path, roots: tuple[Path, ...], workspace_sources: dict[Path, tuple[Path, ...]]
) -> _MigrationPlan:
  workspace_names: dict[str, Path] = {}
  workspaces: list[_WorkspaceMigration] = []
  detached: list[str] = []
  urls_recovered: list[str] = []

  for root, sources in workspace_sources.items():
    checkout = _root_checkout(root, sources)
    for source in sources:
      destination = base / _WORKSPACES / source.name
      other = workspace_names.get(source.name)
      if other is not None:
        raise _collision(source, other)
      if _path_exists(destination):
        raise _collision(source, destination)
      migration = _workspace_migration(source, destination, checkout)
      workspace_names[source.name] = source
      workspaces.append(migration)
      if migration.metadata.get('repo') is None:
        detached.append(source.name)
      if migration.url_recovered:
        urls_recovered.append(source.name)

  planned_files: dict[Path, Path] = {}
  for root in roots:
    for store in root.iterdir():
      if store.name != _WORKSPACES:
        _preflight_store(store, base / store.name, base, planned_files)
  _preflight_trail_ids(base, roots)
  _preflight_summon_request_ids(base, roots)
  return _MigrationPlan(tuple(roots), tuple(workspaces), tuple(detached), tuple(urls_recovered))


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
    if workspace.metadata['isolation'] != Isolation.UNBOXED.value:
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


def _apply_plan(base: Path, plan: _MigrationPlan) -> None:
  repairs = _worktree_repairs(plan)
  if len(repairs) > 0:
    _atomic_json(_pending_worktrees_file(base), {'repairs': repairs})
  for workspace in plan.workspaces:
    if workspace.resume is not None:
      _atomic_json(workspace.source / 'resume.json', workspace.resume)
    _atomic_json(workspace.source / 'workspace.json', workspace.metadata)
    (workspace.source / 'meta.json').unlink(missing_ok=True)
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


def _flat_workspace_sources(base: Path) -> tuple[Path, ...]:
  store = base / _WORKSPACES
  if not store.is_dir():
    return ()
  return tuple(
    path for path in sorted(store.iterdir()) if path.is_dir() and (path / 'meta.json').is_file()
  )


def _preflight_workspace_liveness(migrations: tuple[_WorkspaceMigration, ...]) -> None:
  boxed = {
    workspace.source.name: workspace.source / 'tree'
    for workspace in migrations
    if workspace.metadata['isolation'] == Isolation.BOXED.value
    and (workspace.source / 'tree').is_dir()
  }
  if len(boxed) == 0:
    return
  mounts = _running_mounts()
  for name, tree in boxed.items():
    if str(tree) in mounts:
      raise RuntimeStateMigrationError(
        f'legacy workspace {name!r} is live (container running): {tree}'
      )


def _flat_workspace_migrations(sources: tuple[Path, ...]) -> tuple[_WorkspaceMigration, ...]:
  return tuple(
    _workspace_migration(source, source, None, recover_attachment=False) for source in sources
  )


def _apply_flat_workspace_migrations(migrations: tuple[_WorkspaceMigration, ...]) -> None:
  for workspace in migrations:
    if workspace.resume is not None:
      _atomic_json(workspace.source / 'resume.json', workspace.resume)
    _atomic_json(workspace.source / 'workspace.json', workspace.metadata)
    (workspace.source / 'meta.json').unlink()


def migrate_runtime_state() -> None:
  """Migrate historical stores and workspace records to the current strict shapes."""
  base = runtime_base()
  roots = _legacy_roots(base)
  flat = _flat_workspace_sources(base)
  pending = _pending_worktrees_file(base).is_file()
  if len(roots) == 0 and len(flat) == 0 and not pending:
    return
  plan: Optional[_MigrationPlan] = None
  flat_migrations: tuple[_WorkspaceMigration, ...] = ()
  with _migration_lock(base):
    _repair_pending_worktrees(base)
    roots = _legacy_roots(base)
    workspace_sources = _legacy_workspaces(roots)
    flat_sources = _flat_workspace_sources(base)
    sources = (*chain.from_iterable(workspace_sources.values()), *flat_sources)
    with _hold_workspace_locks(sources):
      if len(roots) > 0:
        plan = _build_plan(base, roots, workspace_sources)
      flat_migrations = _flat_workspace_migrations(flat_sources)
      migrations = (*(() if plan is None else plan.workspaces), *flat_migrations)
      _preflight_workspace_liveness(migrations)
      if plan is not None:
        _apply_plan(base, plan)
      _apply_flat_workspace_migrations(flat_migrations)
  migrated_records = len(flat_migrations)
  if plan is not None:
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
    if len(plan.urls_recovered) > 0:
      log.warning(
        'migrated workspace(s) attached by upstream URL because their legacy root named no '
        'recoverable checkout, so a host config entry keyed on that checkout no longer '
        'reaches them: %s',
        ', '.join(plan.urls_recovered),
      )
  if migrated_records > 0:
    log.info('migrated %d workspace record(s) to workspace.json', migrated_records)
