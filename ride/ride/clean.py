from typing import Optional

from bro.base import log
from bro.workspace.paths import project_root
from ride.workspace.docker import running_mounts
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace


def clean_workspaces(
  force: bool = False, dry_run: bool = False, names: Optional[list[str]] = None
) -> int:
  project = project_root()
  workspaces = Workspace.all(project)

  selected = set(names) if names is not None and len(names) > 0 else None
  if selected is not None:
    missing = selected - {workspace.name for workspace in workspaces}
    if len(missing) > 0:
      log.error('workspace(s) not found: %s', ', '.join(sorted(missing)))
      return 1
    workspaces = [workspace for workspace in workspaces if workspace.name in selected]

  workspaces.sort(key=lambda workspace: (workspace.kind, workspace.name))

  has_containers = any(workspace.kind is WorkspaceKind.CONTAINER for workspace in workspaces)
  mounts = running_mounts() if has_containers else set()

  removed = 0
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
    else:
      try:
        workspace.remove()
      except (RuntimeError, OSError) as error:
        log.error('skip %s: %s', workspace.name, error)
        failed += 1
        continue
      log.info('removed %s', workspace.name)
    removed += 1

  log.info('cleaned %d workspace(s), skipped %d, failed %d', removed, skipped, failed)
  return 1 if failed > 0 else 0
