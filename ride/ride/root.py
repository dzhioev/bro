import subprocess
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace

from bro.base import configs
from ride.runtime_bundle import RuntimeBundle
from ride.workspace.containers import attach_interactive, broker_enabled
from ride.workspace.docker import (
  ContainerRuntimeResolver,
  Launch as DockerLaunch,
  prepare_container,
)
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets, log_scoped_secrets


@dataclass(frozen=True)
class ProcessLaunch:
  """A supervision-neutral process launch with a complete environment snapshot."""

  command: list[str]
  cwd: str
  env: dict[str, str]
  interactive: bool


def _run_via_broker(
  launch: DockerLaunch | ProcessLaunch,
  workspace: Workspace,
  *,
  may_summon: Collection[str],
  summon_depth: int,
  summon_harness: str,
  credential_scope: ScopedSecrets,
  container_runtime: ContainerRuntimeResolver,
  runtime_bundle: RuntimeBundle,
) -> int:
  from bro.summon import MAY_SUMMON_ENV, encode_may_summon
  from ride.artifacts import view_mount
  from ride.spawn import run_root_via_broker
  from ride.workspace.spawn import DockerLaunchSpec, ProcessLaunchSpec

  launch_env = dict(launch.env)
  launch_env[MAY_SUMMON_ENV] = encode_may_summon(may_summon)
  if isinstance(launch, DockerLaunch):
    artifacts_mount = view_mount(workspace.name, workspace.name)
    broker_launch = DockerLaunchSpec(
      replace(
        launch,
        env=launch_env,
        extra_mounts=(*launch.extra_mounts, artifacts_mount),
      ),
      capture_output=False,
    )
  else:
    broker_launch = ProcessLaunchSpec(
      command=launch.command,
      cwd=launch.cwd,
      env=launch_env,
      interactive=launch.interactive,
    )
  return run_root_via_broker(
    broker_launch,
    workspace=workspace,
    bro=launch.env['RIDE_BRO'],
    may_summon=may_summon,
    summon_depth=summon_depth,
    summon_harness=summon_harness,
    credential_scope=credential_scope,
    container_runtime=container_runtime,
    runtime_bundle=runtime_bundle,
  )


def _run_direct(launch: DockerLaunch | ProcessLaunch) -> int:
  if isinstance(launch, ProcessLaunch):
    env = dict(launch.env)
    env.pop('BROKER_CHANNEL', None)
    env.pop('BROKER_UPSTREAM', None)
    return subprocess.run(launch.command, cwd=launch.cwd, env=env).returncode
  container_id = prepare_container(launch)
  if launch.tty:
    return attach_interactive(container_id)
  return subprocess.run(['docker', 'start', '-a', container_id]).returncode


def run_started_party(
  launch: DockerLaunch | ProcessLaunch,
  workspace: Workspace,
  *,
  may_summon: Collection[str] = (),
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH,
  summon_harness: str = configs.DEFAULT_SUMMON_HARNESS,
  credential_scope: ScopedSecrets,
  container_runtime: ContainerRuntimeResolver,
  runtime_bundle: RuntimeBundle,
) -> int:
  """Supervise a started party's first session and record how it ended."""
  log_scoped_secrets(
    workspace.name,
    credential_scope.required,
    credential_scope.optional,
  )
  workspace.clear_session_end()
  if broker_enabled():
    code = _run_via_broker(
      launch,
      workspace,
      may_summon=may_summon,
      summon_depth=summon_depth,
      summon_harness=summon_harness,
      credential_scope=credential_scope,
      container_runtime=container_runtime,
      runtime_bundle=runtime_bundle,
    )
  else:
    code = _run_direct(launch)
  workspace.record_session_end(code)
  return code


def run_manual_started_party(
  launch: DockerLaunch | ProcessLaunch,
  workspace: Workspace,
  *,
  credential_scope: ScopedSecrets,
  claim: Callable[[], object],
) -> int:
  """Run a manually started party on its summoner's provisioned channel."""
  log_scoped_secrets(
    workspace.name,
    credential_scope.required,
    credential_scope.optional,
  )
  workspace.clear_session_end()
  if isinstance(launch, DockerLaunch):
    container_id = prepare_container(launch)
    claim()
    code = attach_interactive(container_id)
  else:
    claim()
    code = subprocess.run(launch.command, cwd=launch.cwd, env=launch.env).returncode
  workspace.record_session_end(code)
  return code
