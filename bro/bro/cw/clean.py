import concurrent.futures
from typing import Optional

from base import log
from cw.docker import running_mounts
from cw.git import git_run, no_prompt_env
from cw.paths import _project_root
from cw.workspace import ContainerWorkspace, Workspace


def clean_workspaces(
  force: bool = False, dry_run: bool = False, refs: Optional[list[str]] = None
) -> int:
  proj = _project_root()
  workspaces = Workspace.all(proj)

  filter_refs = set(refs) if refs is not None and len(refs) > 0 else None
  if filter_refs is not None:
    missing = filter_refs - {ws.ref for ws in workspaces}
    if len(missing) > 0:
      log.error('workspace(s) not found: %s', ', '.join(sorted(missing)))
      return 1
    workspaces = [ws for ws in workspaces if ws.ref in filter_refs]

  workspaces.sort(key=lambda ws: (isinstance(ws, ContainerWorkspace), ws.name))

  # fetch origin/master once up front: the per-workspace checks below run
  # concurrently and share proj's (resp. the common dir's) refs, so a fetch
  # inside each would race on the ref lock. a stale ref only ever makes the
  # ancestry check stricter (errs toward keeping a workspace), so a failed
  # fetch is a warning, not fatal. with this done, each check passes
  # refresh_origin=False and touches only read-only shared state.
  fetched = git_run('fetch', '--quiet', 'origin', 'master', cwd=proj, env=no_prompt_env())
  if fetched.returncode != 0:
    log.warning('could not fetch origin/master; ancestry checks use the local ref')

  def _assess(ws: Workspace, mounts: set[str]) -> tuple[Workspace, bool, bool, list[str]]:
    if ws.is_active(mounts):
      return ws, True, False, []
    safe, reasons = ws.is_clean(refresh_origin=False)
    return ws, False, safe, reasons

  # host worktrees don't need the running-mount set, so assess them concurrently
  # with the `docker ps` mounts fetch; containers are assessed once it resolves.
  host_ws = [ws for ws in workspaces if not isinstance(ws, ContainerWorkspace)]
  container_ws = [ws for ws in workspaces if isinstance(ws, ContainerWorkspace)]
  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(running_mounts) if len(container_ws) > 0 else None
    host_results = list(pool.map(lambda ws: _assess(ws, set()), host_ws))
    mounts = mounts_future.result() if mounts_future is not None else set()
    container_results = list(pool.map(lambda ws: _assess(ws, mounts), container_ws))
  results = host_results + container_results

  removed = 0
  skipped = 0
  failed = 0
  for ws, active, safe, reasons in results:
    if active:
      log.info('skip %s: active session', ws.ref)
      skipped += 1
      continue
    if not safe:
      if not force:
        log.info('skip %s: %s', ws.ref, '; '.join(reasons))
        skipped += 1
        continue
      log.info('force %s: %s', ws.ref, '; '.join(reasons))
    if dry_run:
      log.info('would remove %s', ws.ref)
    else:
      try:
        ws.remove()
      except RuntimeError as e:
        log.error('skip %s: %s', ws.ref, e)
        failed += 1
        continue
      log.info('removed %s', ws.ref)
    removed += 1

  log.info('cleaned %d workspace(s), skipped %d, failed %d', removed, skipped, failed)
  return 1 if failed > 0 else 0
