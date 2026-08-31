import subprocess
from collections.abc import Callable, Collection
from dataclasses import replace

from ride.workspace.containers import attach_interactive, broker_enabled
from ride.workspace.docker import (
  ContainerRuntime,
  ContainerRuntimeResolver,
  Launch,
  prepare_container,
)
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets, log_scoped_secrets


def _run_root_via_broker(
  launch: Launch,
  workspace: Workspace,
  *,
  may_summon: Collection[str],
) -> int:
  """run the container launch as the broker's supervised root peer."""
  # imported here, not at module level: broker_enabled() must be able to
  # short-circuit a launch before anything touches the broker package (see its
  # docstring).
  from bro.summon import MAY_SUMMON_ENV, encode_may_summon
  from ride.artifacts import view_mount
  from ride.spawn import run_root_via_broker
  from ride.workspace.spawn import DockerLaunchSpec

  env = dict(launch.env)
  env[MAY_SUMMON_ENV] = encode_may_summon(may_summon)
  # the mount source — the root's view dir — is created when run_root_via_broker
  # constructs the session store, before the docker launch consumes this spec
  artifacts_mount = view_mount(workspace.name, workspace.name)
  broker_launch = DockerLaunchSpec(
    replace(launch, env=env, extra_mounts=(*launch.extra_mounts, artifacts_mount)),
    capture_output=False,
  )
  container_runtime = ContainerRuntimeResolver.fixed(
    ContainerRuntime(launch.image, launch.runtime_bundle_hash), workspace.repository
  )
  return run_root_via_broker(
    broker_launch,
    workspace=workspace,
    bro=launch.env['RIDE_BRO'],
    may_summon=may_summon,
    credential_scope=ScopedSecrets(
      required=set(launch.secrets),
      optional=set(launch.optional_secrets),
      selection=dict(launch.credential_selection),
    ),
    container_runtime=container_runtime,
  )


def run_host_process_via_broker(
  workspace: Workspace,
  command: list[str],
  env: dict[str, str],
  may_summon: Collection[str],
  credential_scope: ScopedSecrets,
  container_runtime: ContainerRuntimeResolver,
  *,
  bro: str,
  interactive: bool,
) -> int:
  """run a host-worktree process as the broker's supervised session root."""
  from bro.summon import MAY_SUMMON_ENV, encode_may_summon
  from ride.spawn import run_root_via_broker
  from ride.workspace.spawn import ProcessLaunchSpec

  launch_env = dict(env)
  launch_env[MAY_SUMMON_ENV] = encode_may_summon(may_summon)
  launch = ProcessLaunchSpec(
    command=command,
    cwd=str(workspace.tree),
    env=launch_env,
    interactive=interactive,
  )
  return run_root_via_broker(
    launch,
    workspace=workspace,
    bro=bro,
    may_summon=may_summon,
    credential_scope=credential_scope,
    container_runtime=container_runtime,
  )


def run_summoned_in_container(
  launch: Launch, workspace: Workspace, *, claim: Callable[[], object]
) -> int:
  """run a manual summon child's container launch: no broker of its own — its
  `BROKER_UPSTREAM` points at the summoner's provisioned channel for the entrypoint broxy
  — prepared first, the token claimed only once nothing fallible is
  left before the attach, then attached interactively."""
  log_scoped_secrets(launch.name, launch.secrets, launch.optional_secrets)
  workspace.clear_session_end()
  container_id = prepare_container(launch)
  claim()
  code = attach_interactive(container_id)
  workspace.record_session_end(code)
  return code


def run_in_container(
  launch: Launch,
  workspace: Workspace,
  *,
  may_summon: Collection[str] = (),
) -> int:
  """run a prepared launch in `workspace`, directly or as the root peer of a
  bro.broker. The launch description is supervision-neutral. The broker path
  wraps it only after the lazy import gate; the fallback uses the same container
  prepare and attaches with plain `docker start`. `may_summon` configures the
  broker root's outgoing allow-list.
  """
  # the container starts with origin/master only as fresh as the host's last fetch.
  # ancestry-changing workflows fetch again before acting; the remaining reader is informational.
  log_scoped_secrets(launch.name, launch.secrets, launch.optional_secrets)
  workspace.clear_session_end()
  if broker_enabled():
    code = _run_root_via_broker(launch, workspace, may_summon=may_summon)
  else:
    container_id = prepare_container(launch)
    if launch.tty:
      code = attach_interactive(container_id)
    else:
      code = subprocess.run(['docker', 'start', '-a', container_id]).returncode
  workspace.record_session_end(code)
  return code
