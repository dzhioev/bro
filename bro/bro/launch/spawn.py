"""summon lowering and the broker-root composition over the workspace spawners.

`SummonSpawner` resolves the requested base ref off-loop, asks `bro_run.describe`
to construct the target's neutral headless launch, wraps it for the docker
spawner, and marks its channel-named workspace for teardown.

`run_root_via_broker` composes both launch modes and summon lowering under one
broker, then supervises the root until exit.
"""

import asyncio
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from base import log
from bro.launch.bro_run import describe
from bro.launch.summon_control import SummonControl, summon_status_file
from broker.brotocol import Message, Tag
from broker.dispatcher import Broker, Dispatcher, ping_handler
from broker.runtime import Peer
from broker.spawn import ChildHandle, LaunchSpec, Spawner
from broker.transport import Provisioned
from broker.transports.unix import UnixServerTransport
from summon import SUMMON
from workspace.git import resolve_head, resolve_ref
from workspace.paths import broker_dir, host_log_dir, project_root, summon_dir
from workspace.spawn import (
  CompositeSpawner,
  DockerLaunchSpec,
  DockerSpawner,
  ProcessLaunchSpec,
  ProcessSpawner,
)
from workspace.store import log_scoped_secrets


@dataclass(frozen=True)
class SummonLaunchSpec(LaunchSpec):
  """an authorized summon as a launch description, cheap to build on the broker
  loop: the request fields plus the summoner's workspace path (resolved by the
  control at request time — the source the child's default base is read from).
  `SummonSpawner` lowers it to a `DockerLaunchSpec` off-loop — the target-bro
  import, scoped-set computation, and base-ref resolution are all blocking work a
  handler must not run."""

  target: str
  prompt: str
  parent_workspace: Path
  summoner: dict[str, Any]
  into: Optional[str] = None


def _workspace_name(channel: str) -> str:
  return f'broker-{channel}'


def _lower_summon(launch: SummonLaunchSpec, workspace_name: str) -> DockerLaunchSpec:
  """the blocking half of a summon spawn: compute the docker launch a host-side
  `bro run <target>` would get — the shared bro-run description (`bro_run.describe`:
  exactly the target's own scope, nothing inherited from the summoner, no grant
  passthrough). The base is the summoner's workspace HEAD, read live here
  (`resolve_head` — which also transfers the commit's objects into the host repo
  when they live only in the summoner's own store), unless the request's `into`
  names a ref (resolved with the same fetch-if-unresolvable rule as `cw ss
  --into`, but an unresolvable ref fails the spawn rather than falling back).
  Raises on any unresolvable input — the spawner surfaces that as the correlated
  `failed{reason: 'launch'}`."""
  project = project_root()
  if launch.into is not None:
    base_ref = resolve_ref(project, launch.into)
    if base_ref is None:
      raise ValueError(f'cannot resolve summon into ref {launch.into!r}')
  else:
    base_ref = resolve_head(project, launch.parent_workspace)
    if base_ref is None:
      raise ValueError(f"cannot read the summoner's HEAD at {launch.parent_workspace}")
  run = describe(
    launch.target,
    [launch.prompt],
    workspace_name=workspace_name,
    verb='run',
    base_ref=base_ref,
    tty=False,
    forward_env=False,
    summoner=launch.summoner,
  )
  log_scoped_secrets(f'summoned {launch.target}', run.secrets, run.optional_secrets)
  return DockerLaunchSpec(run, remove_workspace=True)


class SummonSpawner(Spawner):
  """lower a `SummonLaunchSpec` to its docker launch off-loop, then delegate to
  the docker path (which runs its own blocking prepare off-loop too)."""

  def __init__(self, docker: DockerSpawner):
    self._docker = docker

  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, SummonLaunchSpec)
    lowered = await asyncio.to_thread(_lower_summon, launch, _workspace_name(channel.channel))
    return await self._docker.spawn(lowered, channel)


def _log_root_started(context: Dispatcher, peer: Peer, message: Message) -> None:
  del context, peer
  log.info('root run started (trail %s)', message.payload.get('trail_id'))


def _log_root_completed(context: Dispatcher, peer: Peer, message: Message) -> None:
  del context, peer
  if message.payload.get('end_reason') == 'raised':
    # a raised run's result is the abort reason — surface it
    log.warning('root run raised: %s', message.payload.get('result'))
    return
  log.info('root run ended: %s', message.payload.get('end_reason'))


def run_root_via_broker(
  launch: LaunchSpec, project: Path, *, session: str, may_summon: Collection[str] = ()
) -> int:
  """run `launch` as the root peer of a broker over the host control dir
  (`var/cw/broker`), supervise it on the broker loop until it exits, and return its
  exit code. The spawner is the composite over both cw launch modes plus the summon
  lowering, so any root — host process or container — can spawn docker children.
  The broker answers the substrate's built-in ping, so a session can verify its
  channel (`broker request ping '{}'`), and logs the root's own run lifecycle
  (`started`/`completed`) as its parent. While an interactive root owns the
  terminal, host output goes to `var/cw/log/<session>.log` instead of the shared
  TTY (see `workspace.spawn._HostLogRedirect`); headless runs keep it on stderr.

  `session` is the session key — the workspace name, mode-prefixed by the launch
  surface (see `bro/launch/summon_control.py`) — the root's identity in the summon audit and the
  key of its per-session state files. `may_summon` names the bros the root session
  is authorized to summon — its effective outgoing allow-list (`bro/launch/summon_control.py`);
  defaults to deny-all. A summoned child follows its own bro's static seeds
  instead, resolved per request by the control. The summon handler is registered
  either way, so a denied summoner always gets a clean correlated error instead of
  a silent refuse; after the loop ends — cleanly or by an exception unwinding out
  of it — children the root's exit killed mid-flight are logged loudly."""
  targets = sorted(set(may_summon))
  if len(targets) > 0:
    log.info('session may summon: %s', ', '.join(targets))
  host_log = host_log_dir(project) / f'{session}.log'
  docker_spawner = DockerSpawner(host_log=host_log)
  spawner = CompositeSpawner(
    {
      DockerLaunchSpec: docker_spawner,
      ProcessLaunchSpec: ProcessSpawner(host_log=host_log),
      SummonLaunchSpec: SummonSpawner(docker_spawner),
    }
  )
  control = SummonControl(
    allow_list=may_summon,
    session=session,
    project=project,
    status_file=summon_status_file(project, session),
    audit_file=summon_dir(project) / f'{session}.jsonl',
  )
  facade = Broker(UnixServerTransport(str(broker_dir(project))), spawner)
  facade.on(Tag.PING, ping_handler)
  # the root's own lifecycle (a bro run at the session root) has no parent peer to
  # route to; this host process is its parent, so it lands in the host log
  facade.on(Tag.STARTED, _log_root_started)
  facade.on(Tag.COMPLETED, _log_root_completed)
  facade.on(SUMMON, control.handle)
  facade.add_delivery_observer(control.observe_delivery)
  try:
    return facade.run(launch)
  finally:
    control.log_killed_in_flight()
