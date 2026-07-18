import concurrent.futures
from typing import Optional

from base import log
from cw.claude_config import drop_workspace
from workspace.docker import running_mounts
from workspace.git import git_run, no_prompt_env
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

  # fetch origin/master once up front: the per-workspace checks below run
  # concurrently and share project's (resp. the common dir's) refs, so a fetch
  # inside each would race on the ref lock. a stale ref only ever makes the
  # ancestry check stricter (errs toward keeping a workspace), so a failed
  # fetch is a warning, not fatal. with this done, each check passes
  # refresh_origin=False and touches only read-only shared state.
  fetched = git_run('fetch', '--quiet', 'origin', 'master', cwd=project, env=no_prompt_env())
  if fetched.returncode != 0:
    log.warning('could not fetch origin/master; ancestry checks use the local ref')

  def _assess(workspace: Workspace, mounts: set[str]) -> tuple[Workspace, bool, bool, list[str]]:
    if workspace.is_active(mounts):
      return workspace, True, False, []
    safe, reasons = workspace.is_clean(refresh_origin=False)
    return workspace, False, safe, reasons

  # host worktrees don't need the running-mount set, so assess them concurrently
  # with the `docker ps` mounts fetch; containers are assessed once it resolves.
  host_workspaces = [
    workspace for workspace in workspaces if not isinstance(workspace, ContainerWorkspace)
  ]
  container_workspaces = [
    workspace for workspace in workspaces if isinstance(workspace, ContainerWorkspace)
  ]
  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(running_mounts) if len(container_workspaces) > 0 else None
    host_results = list(pool.map(lambda workspace: _assess(workspace, set()), host_workspaces))
    mounts = mounts_future.result() if mounts_future is not None else set()
    container_results = list(
      pool.map(lambda workspace: _assess(workspace, mounts), container_workspaces)
    )
  results = host_results + container_results

  removed = 0
  skipped = 0
  failed = 0
  for workspace, active, safe, reasons in results:
    if active:
      log.info('skip %s: active session', workspace.ref)
      skipped += 1
      continue
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
