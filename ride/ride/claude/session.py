import os
import subprocess
from typing import Optional

from bro.base import credentials, log
from bro.launch.root import run_host_process_via_broker, run_in_container
from bro.launch.trails import local_trails_mounts
from bro.workspace.containers import broker_enabled
from bro.workspace.docker import Launch, find_container_id
from bro.workspace.model import Workspace
from bro.workspace.paths import project_root, venv_env
from bro.workspace.store import log_scoped_secrets, materialize_scoped_store
from bro.workspace.worktrees import ensure_host_worktree, provision_host_worktree
from ride.claude.claude_auth import _apply_claude_auth
from ride.claude.claude_config import (
  _latest_jsonl,
  _provision_host_claude_dir,
  container_claude_state,
  session_trail_pointer,
  workspace_projects_dir,
)
from ride.session import ScopedLaunch, SessionSpec


def launch_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  *,
  container: bool,
) -> int:
  if container:
    return _container_session(spec, workspace, base_ref, launch_scope)
  return _host_session(spec, workspace, base_ref, launch_scope)


def _container_session(
  spec: SessionSpec, workspace: Workspace, base_ref: Optional[str], launch_scope: ScopedLaunch
) -> int:
  if find_container_id(workspace.tree) is not None:
    log.error(
      'session already active in the container for workspace %r; refusing to start a second',
      spec.name,
    )
    return 1

  if spec.resume:
    projects_dir = workspace_projects_dir(workspace)
    if _latest_jsonl(projects_dir) is None:
      log.error('no claude session found for %s in %s', spec.name, projects_dir)
      return 1

  scoped = launch_scope.scoped
  env: dict[str, str] = {'CW_BRO': spec.session_bro}
  if base_ref is not None:
    env['CW_BASE_REF'] = base_ref
  claude_mounts, claude_env = container_claude_state(spec.name)
  env.update(claude_env)
  trails_mounts = local_trails_mounts(scoped)
  launch = Launch(
    name=spec.name,
    command=spec.inner_command(),
    env=env,
    secrets=scoped.required,
    docker_sock=scoped.docker_sock,
    tty=not spec.solo,
    forward_env=True,
    optional_secrets=scoped.optional,
    extra_mounts=(*claude_mounts, *trails_mounts),
  )
  return run_in_container(
    launch,
    workspace=workspace,
    may_summon=launch_scope.may_summon,
    trail_pointer=session_trail_pointer(spec.name),
  )


def _host_session(
  spec: SessionSpec, workspace: Workspace, base_ref: Optional[str], launch_scope: ScopedLaunch
) -> int:
  project = project_root()
  os.chdir(project)
  worktree = workspace.tree
  scoped = launch_scope.scoped

  if spec.resume and _latest_jsonl(workspace_projects_dir(workspace)) is None:
    log.error('no claude session found for %s in %s', spec.name, workspace_projects_dir(workspace))
    return 1

  log_scoped_secrets(spec.name, scoped.required, scoped.optional)
  if not ensure_host_worktree(worktree, workspace.metadata.branch, base_ref):
    return 1
  if not provision_host_worktree(worktree):
    return 1

  ride_binary = worktree / '.venv' / 'bin' / 'ride'
  if not ride_binary.is_file():
    log.error(
      'no ride in %s — the worktree base predates `ride along --in-place`; '
      'rebase it onto origin/master or recreate it',
      ride_binary,
    )
    return 1

  command = [str(ride_binary), *spec.inner_command()[1:]]
  runner_env = venv_env(worktree / '.venv')
  claude_dir = _provision_host_claude_dir(spec.name, worktree, project)
  runner_env['CLAUDE_CONFIG_DIR'] = str(claude_dir)
  runner_env[credentials.REGISTRY_ENV] = str(
    materialize_scoped_store(launch_scope.store, claude_dir / '.bro')
  )
  _apply_claude_auth(runner_env)
  workspace.clear_session_end()
  if broker_enabled():
    code = run_host_process_via_broker(
      workspace,
      command,
      runner_env,
      launch_scope.may_summon,
      scoped.required | scoped.optional,
      interactive=not spec.solo,
      trail_pointer=session_trail_pointer(workspace.name),
    )
  else:
    code = subprocess.run(command, cwd=str(worktree), env=runner_env).returncode
  workspace.record_session_end(code)
  return code
