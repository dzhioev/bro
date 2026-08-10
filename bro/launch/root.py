import subprocess
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.workspace.containers import attach_interactive, container_broker_enabled
from bro.workspace.docker import Launch, prepare_container
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from bro.workspace.paths import project_root
from bro.workspace.store import log_scoped_secrets


def _run_root_via_broker(
  launch: Launch,
  workspace: Workspace,
  *,
  may_summon: Collection[str],
  trail_pointer: Optional[Path],
) -> int:
  """run the container launch as the broker's supervised root peer."""
  # imported here, not at module level: container_broker_enabled() must be able to
  # short-circuit a launch before anything touches the broker package (see its
  # docstring).
  from bro.launch.spawn import run_root_via_broker
  from bro.launch.summon_control import STATUS_ENV, container_status_path
  from bro.workspace.spawn import DockerLaunchSpec

  env = dict(launch.env)
  env[STATUS_ENV] = container_status_path(workspace.project, workspace.name)
  broker_launch = DockerLaunchSpec(replace(launch, env=env))
  return run_root_via_broker(
    broker_launch,
    workspace=workspace,
    may_summon=may_summon,
    credential_scope=set(launch.secrets) | set(launch.optional_secrets),
    trail_pointer=trail_pointer,
  )


def run_in_container(
  launch: Launch,
  *,
  workspace: Optional[Workspace] = None,
  drop: bool = False,
  may_summon: Collection[str] = (),
  trail_pointer: Optional[Path] = None,
) -> int:
  """run a prepared launch directly or as the root peer of a bro.broker.The launch description is supervision-neutral. The broker path wraps it only
  after the lazy import gate; the fallback uses the same container prepare and
  attaches with plain `docker start`. `workspace` is the launch's workspace when
  the caller already recorded one; otherwise it is recorded here. `drop` removes
  it after a clean exit — a failed run keeps it on disk for inspection and
  recovery — and `may_summon` configures the broker root's outgoing allow-list.
  """
  # the container starts with origin/master only as fresh as the host's last fetch.
  # ancestry-changing workflows fetch again before acting; the remaining reader is informational.
  project = project_root()
  log_scoped_secrets(launch.name, launch.secrets, launch.optional_secrets)
  if workspace is None:
    workspace = Workspace.ensure(launch.name, project, WorkspaceKind.CONTAINER)
  workspace.clear_session_end()
  if container_broker_enabled():
    code = _run_root_via_broker(
      launch, workspace, may_summon=may_summon, trail_pointer=trail_pointer
    )
  else:
    container_id = prepare_container(launch, project)
    if launch.tty:
      code = attach_interactive(container_id)
    else:
      code = subprocess.run(['docker', 'start', '-a', container_id]).returncode
  workspace.record_session_end(code)
  if drop:
    if code == 0:
      try:
        workspace.remove()
        log.info('removed container workspace %s', launch.name)
      except (RuntimeError, OSError) as e:
        log.warning('could not fully remove container workspace %s: %s', launch.name, e)
    else:
      log.info('run exited with code %d; keeping container workspace %s', code, launch.name)
  return code
