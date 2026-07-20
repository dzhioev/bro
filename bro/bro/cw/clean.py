from typing import Optional

from base import log
from cw.claude_config import drop_workspace
from workspace.docker import running_mounts
from workspace.model import ContainerWorkspace, Workspace
from workspace.paths import project_root


def clean_workspaces(
  force: bool = False, dry_run: bool = False, refs: Optional[list[str]] = None
) -> int:
  project = project_root()
  workspaces = Workspace.all(project)

  filter_refs = set(refs) if refs is not None and len(refs) > 0 else None
  if filter_refs is not None:
    missing = filter_refs - {workspace.ref for workspace in workspaces}
    if len(missing) > 0:
      log.error('workspace(s) not found: %s', ', '.join(sorted(missing)))
      return 1
    workspaces = [workspace for workspace in workspaces if workspace.ref in filter_refs]

  workspaces.sort(key=lambda workspace: (isinstance(workspace, ContainerWorkspace), workspace.name))

  has_containers = any(isinstance(workspace, ContainerWorkspace) for workspace in workspaces)
  mounts = running_mounts() if has_containers else set()

  removed = 0
  skipped = 0
  failed = 0
  for workspace in workspaces:
    if workspace.is_active(mounts):
      log.info('skip %s: active session', workspace.ref)
      skipped += 1
      continue
    safe, reasons = workspace.is_clean()
    if not safe:
      if not force:
        log.info('skip %s: %s', workspace.ref, '; '.join(reasons))
        skipped += 1
        continue
      log.info('force %s: %s', workspace.ref, '; '.join(reasons))
    if dry_run:
      log.info('would remove %s', workspace.ref)
    else:
      try:
        drop_workspace(workspace)
      except RuntimeError as e:
        log.error('skip %s: %s', workspace.ref, e)
        failed += 1
        continue
      log.info('removed %s', workspace.ref)
    removed += 1

  log.info('cleaned %d workspace(s), skipped %d, failed %d', removed, skipped, failed)
  return 1 if failed > 0 else 0
