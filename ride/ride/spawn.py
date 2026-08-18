"""summon lowering and the broker-root composition over the workspace spawners.

`SummonSpawner` resolves the requested base ref off-loop, records the child's
session spec as its channel-named workspace's resume record, derives the child's
inner argv from that spec through the bro harness seam, asks `bro_run.describe`
to wrap it into the target's neutral headless launch for the docker spawner, and
marks the workspace throwaway (removed after a clean exit).

`run_root_via_broker` composes both launch modes and summon lowering under one
broker, then supervises the root until exit.
"""

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from bro.base import log
from bro.broker.brotocol import Message, Tag
from bro.broker.dispatcher import Broker, Dispatcher, ping_handler
from bro.broker.runtime import Peer
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import Provisioned
from bro.broker.transports.unix import UnixServerTransport
from bro.monitor import trail_pointer
from bro.summon import MAY_SUMMON_ENV, SUMMON, encode_may_summon
from bro.workspace.git import resolve_head, resolve_ref
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from bro.workspace.paths import broker_dir, project_root, summon_dir, workspace_dir
from bro.workspace.spawn import (
  CompositeSpawner,
  DockerLaunchSpec,
  DockerSpawner,
  ProcessLaunchSpec,
  ProcessSpawner,
)
from bro.workspace.store import log_scoped_secrets
from ride.bro_run import describe
from ride.flags import default_hold
from ride.harness import get_harness
from ride.scope import split_scope_overrides, summoned_credential_scope
from ride.session import SessionSpec, record_resume_spec
from ride.summon_control import SummonControl, summon_status_file


@dataclass(frozen=True)
class SummonLaunchSpec(LaunchSpec):
  """an authorized summon as a launch description, cheap to build on the broker
  loop: the request fields plus the summoner's workspace path (resolved by the
  control at request time — the source the child's default base is read from).
  `SummonSpawner` lowers it to a `DockerLaunchSpec` off-loop — the target-bro
  import, scoped-set computation, and base-ref resolution are all blocking work
  the broker loop should not carry.

  `grant`/`revoke` are the request's unified values: the credential halves feed
  the child's scope and the whole lists its recorded session spec, while the
  control already resolved the `@bro` halves into `may_summon`, the child's own
  effective allow-list — never the summoner's, which the child is not authorized
  against."""

  target: str
  prompt: str
  parent_workspace: Path
  summoner: Optional[dict[str, Any]]
  may_summon: tuple[str, ...]
  into: Optional[str] = None
  hold: Optional[str] = None
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  llm: Optional[str] = None


def _workspace_name(channel: str) -> str:
  return f'broker-{channel}'


def _child_session_spec(launch: SummonLaunchSpec, workspace_name: str) -> SessionSpec:
  """the summoned child's run as a `SessionSpec`: an unpinned solo container
  session of the target bro under the request's fields — only the request's
  `timeout` maps to no spec field (it is the spawner's wait timer, not part of
  the run). Recorded as the workspace's resume record and the source of the
  child's inner argv, so what `ride resume` relaunches is what ran."""
  harness = get_harness('bro')
  return SessionSpec(
    name=workspace_name,
    harness=harness.name,
    workspace_pinned=False,
    host=False,
    drop=True,
    no_trails=False,
    hold=launch.hold if launch.hold is not None else default_hold(solo=True, host=False),
    grant=list(launch.grant),
    revoke=list(launch.revoke),
    llm=launch.llm,
    resolved_llm=harness.resolve_llm(launch.llm, launch.target).dump(),
    solo=True,
    resume=False,
    into=launch.into,
    bro=launch.target,
    prompt=launch.prompt,
    subject=launch.prompt,
    arguments=[],
    harness_options={},
  )


def _lower_summon(launch: SummonLaunchSpec, workspace_name: str) -> DockerLaunchSpec:
  """the blocking half of a summon spawn: compute the docker launch a host-side
  `bro run <target>` would get — the shared bro-run description (`bro_run.describe`:
  the target's own scope, nothing inherited from the summoner, plus whatever the
  request's own grant/revoke names) around the inner argv of the child's session
  spec. The base is the summoner's workspace HEAD, read live here (`resolve_head`
  — which also transfers the commit's objects into the host repo when they live
  only in the summoner's own store), unless the request's `into` names a ref
  (resolved with the same fetch-if-unresolvable rule as `ride --into`, but an
  unresolvable ref fails the spawn rather than falling back). The child's
  workspace is recorded throwaway, so its supervisor removes it once the child
  exits cleanly. Raises on any unresolvable input — the spawner surfaces that as
  the correlated `failed{reason: 'launch'}`; every fallible resolution precedes
  the workspace record, so a failed spawn creates none."""
  project = project_root()
  if launch.into is not None:
    base_ref = resolve_ref(project, launch.into)
    if base_ref is None:
      raise ValueError(f'cannot resolve summon into ref {launch.into!r}')
  else:
    base_ref = resolve_head(project, launch.parent_workspace)
    if base_ref is None:
      raise ValueError(f"cannot read the summoner's HEAD at {launch.parent_workspace}")
  spec = _child_session_spec(launch, workspace_name)
  grant_credentials, _ = split_scope_overrides(spec.grant)
  revoke_credentials, _ = split_scope_overrides(spec.revoke)
  scoped = summoned_credential_scope(
    launch.target, grant=grant_credentials, revoke=revoke_credentials, llm_spec=spec.llm_spec
  )
  workspace = Workspace.ensure(workspace_name, project, WorkspaceKind.CONTAINER, throwaway=True)
  record_resume_spec(workspace, spec)
  run = describe(
    launch.target,
    get_harness(spec.harness).inner_command(spec, workspace),
    workspace_name=workspace_name,
    scoped=scoped,
    base_ref=base_ref,
    tty=False,
    forward_env=False,
    summoner=launch.summoner,
  )
  log_scoped_secrets(f'summoned {launch.target}', run.secrets, run.optional_secrets)
  env = {**run.env, MAY_SUMMON_ENV: encode_may_summon(launch.may_summon)}
  return DockerLaunchSpec(replace(run, env=env))


class SummonSpawner(Spawner):
  """lower a `SummonLaunchSpec` to its docker launch off-loop, then delegate to
  the docker path (which runs its own blocking prepare off-loop too)."""

  def __init__(self, docker: DockerSpawner):
    self._docker = docker

  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, SummonLaunchSpec)
    lowered = await asyncio.to_thread(_lower_summon, launch, _workspace_name(channel.channel))
    return await self._docker.spawn(lowered, channel)


def _note_root_started(control: SummonControl, workspace: Workspace):
  def _handle(context: Dispatcher, peer: Peer, message: Message) -> None:
    del context, peer
    trail_id = message.payload.get('trail_id')
    log.info('root run started (trail %s)', trail_id)
    # a bro-run root's own trail is what its summon children are attributed to
    control.note_root_trail(trail_id)
    if isinstance(trail_id, str) and len(trail_id) > 0:
      trail_pointer.write(trail_pointer.broker_pointer(workspace.path), trail_id)

  return _handle


def _note_child_started(project: Path):
  """publish each summoned child's `started` trail id as its channel-named
  workspace's broker trail pointer — what makes a failed child's surviving
  workspace resumable. Registered as a delivery observer, which sees only
  correlated child deliveries — never the root's own `started`, so no pointer is
  fabricated for a workspace that doesn't exist."""

  def _observe(source: Optional[Peer], target: Peer, message: Message) -> None:
    del target
    if message.type != Tag.STARTED or source is None:
      return
    trail_id = message.payload.get('trail_id')
    if isinstance(trail_id, str) and len(trail_id) > 0:
      pointer = trail_pointer.broker_pointer(workspace_dir(project, _workspace_name(source)))
      trail_pointer.write(pointer, trail_id)

  return _observe


def _log_root_completed(context: Dispatcher, peer: Peer, message: Message) -> None:
  del context, peer
  if message.payload.get('end_reason') == 'raised':
    # a raised run's result is the abort reason — surface it
    log.warning('root run raised: %s', message.payload.get('result'))
    return
  log.info('root run ended: %s', message.payload.get('end_reason'))


def run_root_via_broker(
  launch: LaunchSpec,
  *,
  workspace: Workspace,
  may_summon: Collection[str] = (),
  credential_scope: Collection[str] = (),
) -> int:
  """run `launch` as the root peer of a broker over the host control dir
  (`bro.workspace.paths.broker_dir`), supervise it on the broker loop until it exits,
  and return its exit code. The spawner is the composite over both ride launch modes plus the summon
  lowering, so any root — host process or container — can spawn docker children.
  The broker answers the substrate's built-in ping, so a session can verify its
  channel (`broker request ping '{}'`), and logs the root's own run lifecycle
  (`started`/`completed`) as its parent. While an interactive root owns the
  terminal, host output goes to the workspace's host log instead of the shared
  TTY (see `bro.workspace.spawn._HostLogRedirect`); headless runs keep it on stderr.

  `workspace` is the workspace the root session runs in — its name is the root's
  identity in the summon audit, and its records carry the broker-published
  current-trail pointer written from the root's `started` lifecycle
  (`bro.monitor.trail_pointer`). `may_summon` names the bros the root session
  is authorized to summon — its effective outgoing allow-list (`ride/ride/summon_control.py`);
  defaults to deny-all. `credential_scope` names the secrets the root session
  was launched with, the bound on what its summons may grant a child; defaults
  to grant-nothing. A summoned child follows its own bro's static seeds
  instead, resolved per request by the control. The summon handler is registered
  either way, so a denied summoner always gets a clean correlated error instead of
  a silent refuse; after the loop ends — cleanly or by an exception unwinding out
  of it — children the root's exit killed mid-flight are logged loudly."""
  targets = sorted(set(may_summon))
  if len(targets) > 0:
    log.info('session may summon: %s', ', '.join(targets))
  project = workspace.project
  host_log = workspace.host_log
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
    credential_scope=credential_scope,
    workspace=workspace,
    status_file=summon_status_file(project, workspace.name),
    audit_file=summon_dir(project) / f'{workspace.name}.jsonl',
  )
  facade = Broker(UnixServerTransport(str(broker_dir(project))), spawner)
  facade.on(Tag.PING, ping_handler)
  # the root's own lifecycle (a bro run at the session root) has no parent peer to
  # route to; this host process is its parent, so it lands in the host log
  facade.on(Tag.STARTED, _note_root_started(control, workspace))
  facade.on(Tag.COMPLETED, _log_root_completed)
  facade.on(SUMMON, control.handle)
  facade.add_delivery_observer(control.observe_delivery)
  facade.add_delivery_observer(_note_child_started(project))
  try:
    return facade.run(launch)
  finally:
    control.log_killed_in_flight()
