from typing import Optional

from bro.base import log
from ride.repository import clean_managed_mirrors
from ride.runtime_bundle import clean_runtime_bundles
from ride.workspace.docker import running_mounts
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace


def clean_workspaces(
  force: bool = False, dry_run: bool = False, names: Optional[list[str]] = None
) -> int:
  workspaces = Workspace.all()

  selected = set(names) if names is not None and len(names) > 0 else None
  if selected is not None:
    missing = selected - {workspace.name for workspace in workspaces}
    if len(missing) > 0:
      log.error('workspace(s) not found: %s', ', '.join(sorted(missing)))
      return 1
    workspaces = [workspace for workspace in workspaces if workspace.name in selected]

  workspaces.sort(key=lambda workspace: (workspace.kind, workspace.name))

  has_containers = any(workspace.kind is WorkspaceKind.CONTAINER for workspace in workspaces)
  try:
    mounts = running_mounts() if has_containers else set()
  except (OSError, RuntimeError) as error:
    log.error('cannot check for active sessions: %s', error)
    return 1

  removed = 0
  removed_names: set[str] = set()
  skipped = 0
  failed = 0
  for workspace in workspaces:
    if workspace.is_active(mounts):
      log.info('skip %s: active session', workspace.name)
      skipped += 1
      continue
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
      try:
        workspace.remove(force=force)
      except (RuntimeError, OSError) as error:
        log.error('skip %s: %s', workspace.name, error)
        failed += 1
        continue
      log.info('removed %s', workspace.name)
    removed += 1

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
