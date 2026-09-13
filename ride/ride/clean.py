import contextlib
from typing import Optional

from bro.base import log
from bro.workspace.paths import workspace_dir
from ride.repository import clean_managed_mirrors
from ride.runtime_bundle import clean_runtime_bundles
from ride.workspace.docker import running_mounts
from ride.workspace.metadata import Isolation, UnrecognizedWorkspaceRecord, WorkspaceNotFound
from ride.workspace.model import (
  Workspace,
  WorkspaceActive,
  hold_workspace_removal,
  remove_workspace_path,
  workspace_path_is_active,
)


def clean_workspaces(
  force: bool = False, dry_run: bool = False, names: Optional[list[str]] = None
) -> int:
  selected = set(names) if names is not None and len(names) > 0 else None
  unrecognized = []
  if selected is None:
    workspaces = Workspace.all()
    missing = []
  else:
    workspaces = []
    missing = []
    for name in sorted(selected):
      try:
        workspaces.append(Workspace.open(name))
      except UnrecognizedWorkspaceRecord:
        if not force:
          raise
        unrecognized.append(workspace_dir(name))
      except WorkspaceNotFound:
        missing.append(name)
  if len(missing) > 0:
    log.error('workspace(s) not found: %s', ', '.join(missing))
    return 1

  workspaces.sort(key=lambda workspace: (workspace.isolation, workspace.name))

  has_containers = len(unrecognized) > 0 or any(
    workspace.isolation is Isolation.BOXED for workspace in workspaces
  )
  try:
    mounts = running_mounts() if has_containers else set()
  except (OSError, RuntimeError) as error:
    log.error('cannot check for active sessions: %s', error)
    return 1

  removed = 0
  removed_names: set[str] = set()
  skipped = 0
  failed = 0
  for path in unrecognized:
    if dry_run:
      if workspace_path_is_active(path, mounts):
        log.info('skip %s: active session', path.name)
        skipped += 1
      else:
        log.info('would remove %s', path.name)
        removed_names.add(path.name)
        removed += 1
      continue
    try:
      with hold_workspace_removal(path, mounts):
        remove_workspace_path(path)
        log.info('removed %s', path.name)
        removed += 1
    except WorkspaceActive:
      log.info('skip %s: active session', path.name)
      skipped += 1
    except (RuntimeError, OSError) as error:
      log.error('skip %s: %s', path.name, error)
      failed += 1

  for workspace in workspaces:
    if dry_run and workspace.is_active(mounts):
      log.info('skip %s: active session', workspace.name)
      skipped += 1
      continue
    try:
      context = (
        contextlib.nullcontext() if dry_run else hold_workspace_removal(workspace.path, mounts)
      )
      with context:
        safe, reasons = workspace.is_clean()
        if not safe:
          if not force:
            log.info('skip %s: %s', workspace.name, '; '.join(reasons))
            skipped += 1
            continue
          log.info('force %s: %s', workspace.name, '; '.join(reasons))
        if dry_run:
          log.info('would remove %s', workspace.name)
          removed_names.add(workspace.name)
        else:
          workspace.remove(force=force)
          log.info('removed %s', workspace.name)
        removed += 1
    except WorkspaceActive:
      log.info('skip %s: active session', workspace.name)
      skipped += 1
    except (RuntimeError, OSError) as error:
      log.error('skip %s: %s', workspace.name, error)
      failed += 1

  remaining = (
    Workspace.all()
    if not dry_run
    else [workspace for workspace in Workspace.all() if workspace.name not in removed_names]
  )
  referenced = {
    workspace.metadata.repo for workspace in remaining if workspace.metadata.repo is not None
  }
  mirror_removed, mirror_skipped = clean_managed_mirrors(referenced, dry_run=dry_run)
  runtime_removed, runtime_skipped = clean_runtime_bundles(dry_run=dry_run)
  action = 'would clean' if dry_run else 'cleaned'
  log.info('%s %d managed mirror(s), skipped %d referenced', action, mirror_removed, mirror_skipped)
  log.info('%s %d runtime bundle(s), skipped %d active', action, runtime_removed, runtime_skipped)
  log.info('cleaned %d workspace(s), skipped %d, failed %d', removed, skipped, failed)
  return 1 if failed > 0 else 0
