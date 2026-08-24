"""summon lowering and the broker-root composition over the workspace spawners.

`SummonSpawner` resolves the requested base ref off-loop, records the child's
session spec as its channel-named workspace's resume record, derives the child's
inner argv and container extras from that spec through the harness seam, wraps
them into the child's headless docker launch, and marks the workspace throwaway
(removed after a clean exit).

`run_root_via_broker` composes both launch modes and summon lowering under one
broker, then supervises the root until exit.
"""

import asyncio
import contextlib
import json
import socket
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from bro.artifact import GET, MINT
from bro.base import log
from bro.broker.brotocol import Message, Tag
from bro.broker.dispatcher import PING, Broker, ping_handler
from bro.broker.runtime import Peer
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.kinds import KindContext
from bro.monitor import trail_pointer
from bro.summon import (
  DEFAULT_HARNESS,
  MAY_SUMMON_ENV,
  SUMMON,
  SUMMONED_ENV,
  SUMMONER_ENV,
  encode_may_summon,
)
from bro.workspace.git import resolve_head, resolve_ref
from bro.workspace.paths import summon_dir, workspace_dir, workspace_tree
from ride.artifacts import ArtifactControl, ArtifactStore, JobArtifacts, view_mount
from ride.flags import default_hold
from ride.harness import ContainerExtras, get_harness
from ride.identity import human_git_identity_env
from ride.inner import inner_command
from ride.kinds import extension_kinds
from ride.peers import Peers
from ride.repository import Repository, as_repository
from ride.scope import split_scope_overrides, summoned_credential_scope
from ride.session import SessionSpec, record_resume_spec
from ride.summon_control import SummonControl, summon_status_file
from ride.trails import local_trails_mounts
from ride.workspace.docker import (
  ContainerRuntime,
  ContainerRuntimeResolver,
  Launch,
  bridge_gateway,
)
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace
from ride.workspace.spawn import (
  CompositeSpawner,
  DockerLaunchSpec,
  DockerSpawner,
  ProcessLaunchSpec,
  ProcessSpawner,
)
from ride.workspace.store import ScopedSecrets, log_scoped_secrets


@dataclass(frozen=True)
class SummonLaunchSpec(LaunchSpec):
  """an authorized summon as a launch description, cheap to build on the broker
  loop: the request fields plus the summoner's workspace name (attributed by the
  control at request time — the source the child's default base is read from).
  `SummonSpawner` lowers it to a `DockerLaunchSpec` off-loop — the target-bro
  import, scoped-set computation, and base-ref resolution are all blocking work
  the broker loop should not carry.

  `grant`/`revoke` are the request's unified values: the credential halves feed
  the child's scope and the whole lists its recorded session spec, while the
  control already resolved the `@bro` halves into `may_summon`, the child's own
  effective allow-list — never the summoner's, which the child is not authorized
  against. `share` names artifact refs the control already checked against the
  summoner's own reach; the lowering links them into the child's view."""

  target: str
  prompt: str
  parent: str
  summoner: Optional[dict[str, Any]]
  may_summon: tuple[str, ...]
  repo: Optional[Repository | Path] = None
  into: Optional[str] = None
  hold: Optional[str] = None
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  share: tuple[str, ...] = ()
  llm: Optional[str] = None
  harness: Optional[str] = None


def _workspace_name(channel: str) -> str:
  return f'broker-{channel}'


def _child_session_spec(launch: SummonLaunchSpec, workspace_name: str) -> SessionSpec:
  """the summoned child's run as a `SessionSpec`: an unpinned solo container
  session of the target bro under the request's fields — only the request's
  `timeout` maps to no spec field (it is the spawner's wait timer, not part of
  the run). Recorded as the workspace's resume record and the source of the
  child's inner argv, so what `ride resume` relaunches is what ran."""
  harness = get_harness(launch.harness if launch.harness is not None else DEFAULT_HARNESS)
  return SessionSpec(
    name=workspace_name,
    repo=None if launch.repo is None else as_repository(launch.repo).identity,
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
    harness_options=harness.default_options(),
  )


def _child_launch(
  spec: SessionSpec,
  command: list[str],
  extras: ContainerExtras,
  *,
  scoped: ScopedSecrets,
  repository: Optional[Repository | Path],
  human_env: dict[str, str],
  base_ref: Optional[str],
  summoner: Optional[dict[str, Any]],
  may_summon: tuple[str, ...],
  artifacts_mount: str,
  container_runtime: ContainerRuntime,
) -> Launch:
  """a summoned child's container launch around its harness-composed inner
  command and extras: the child facts (`RIDE_SUMMONED`, its own reconstructed
  `RIDE_COMMAND`, base ref, provenance, allow-list, the human it works for) over
  an explicit env — nothing forwarded from the spawning process — with no TTY."""
  env = dict(extras.env)
  env.update(human_env)
  env['RIDE_BRO'] = spec.bro
  env['RIDE_COMMAND'] = ' '.join(spec.to_command_argv())
  env[SUMMONED_ENV] = '1'
  if base_ref is not None:
    env['RIDE_BASE_REF'] = base_ref
  env[MAY_SUMMON_ENV] = encode_may_summon(may_summon)
  if summoner is not None:
    env[SUMMONER_ENV] = json.dumps(summoner, ensure_ascii=False, separators=(',', ':'))
  return Launch(
    name=spec.name,
    command=list(command),
    env=env,
    secrets=set(scoped.required),
    optional_secrets=set(scoped.optional),
    tty=False,
    forward_env=False,
    image=container_runtime.image,
    runtime_bundle_hash=container_runtime.bundle_hash,
    extra_mounts=(*extras.mounts, *local_trails_mounts(scoped), artifacts_mount),
    repo=repository,
  )


def _lower_summon(
  launch: SummonLaunchSpec,
  workspace_name: str,
  container_runtime: ContainerRuntimeResolver,
  artifacts: ArtifactStore,
) -> DockerLaunchSpec:
  """the blocking half of a summon spawn: compose the child's docker launch —
  the target's own scope, nothing inherited from the summoner, plus whatever the
  request's own grant/revoke names — around the inner command and container
  extras of the child's session spec, both supplied by its harness. The base is
  the summoner's workspace HEAD, read live here (`resolve_head` — which also
  transfers the commit's objects into the host repo when they live only in the
  summoner's own store), unless the request's `into` names a ref (resolved with
  the same fetch-if-unresolvable rule as `ride --into`, but an unresolvable ref
  fails the spawn rather than falling back). The child's workspace is recorded
  throwaway, so its supervisor removes it once the child exits cleanly. The
  child's artifact view is created (and the request's `share` refs linked into
  it) here, where the workspace name exists, before the mount that serves it.
  Raises on any unresolvable input — the spawner surfaces that as the
  correlated `failed{reason: 'launch'}`; every fallible resolution precedes the
  workspace record, so a failed spawn creates none."""
  repo = None if launch.repo is None else as_repository(launch.repo)
  if launch.into is not None:
    if repo is None:
      raise ValueError('summon into requires an attached repository')
    base_ref = resolve_ref(repo.git_dir, launch.into)
    if base_ref is None:
      raise ValueError(f'cannot resolve summon into ref {launch.into!r}')
  elif repo is None:
    base_ref = None
  else:
    parent_tree = workspace_tree(launch.parent)
    base_ref = resolve_head(repo.git_dir, parent_tree)
    if base_ref is None:
      raise ValueError(f"cannot read the summoner's HEAD at {parent_tree}")
  spec = _child_session_spec(launch, workspace_name)
  harness = get_harness(spec.harness)
  auth_error = harness.preflight_auth(spec)
  if auth_error is not None:
    raise ValueError(auth_error)
  grant_credentials, _ = split_scope_overrides(spec.grant)
  revoke_credentials, _ = split_scope_overrides(spec.revoke)
  scoped = summoned_credential_scope(
    launch.target,
    harness.scope_recipe(spec.harness_options),
    attachment=None if repo is None else repo.identity,
    grant=grant_credentials,
    revoke=revoke_credentials,
    llm_spec=spec.llm_spec,
  )
  resolved_runtime = container_runtime.resolve()
  workspace = Workspace.ensure(workspace_name, repo, WorkspaceKind.CONTAINER, throwaway=True)
  record_resume_spec(workspace, spec)
  artifacts.view(workspace_name)
  artifacts.share(launch.share, to=workspace_name, by=launch.parent)
  run = _child_launch(
    spec,
    inner_command(spec, harness_flags=harness.inner_flags(spec)),
    harness.container_extras(spec, workspace, scoped),
    scoped=scoped,
    repository=launch.repo,
    human_env=human_git_identity_env(repo),
    base_ref=base_ref,
    summoner=launch.summoner,
    may_summon=launch.may_summon,
    artifacts_mount=view_mount(artifacts.session, workspace_name),
    container_runtime=resolved_runtime,
  )
  log_scoped_secrets(f'summoned {launch.target}', run.secrets, run.optional_secrets)
  return DockerLaunchSpec(run)


class SummonSpawner(Spawner):
  """lower a `SummonLaunchSpec` to its docker launch off-loop, then delegate to
  the docker path (which runs its own blocking prepare off-loop too). The
  child's workspace name is noted into the peer registry first, on the loop —
  the child cannot connect before its launch resolves, so attribution never
  finds it unnamed."""

  def __init__(
    self,
    docker: DockerSpawner,
    container_runtime: ContainerRuntimeResolver,
    peers: Peers,
    artifacts: ArtifactStore,
  ):
    self._docker = docker
    self._container_runtime = container_runtime
    self._peers = peers
    self._artifacts = artifacts

  async def spawn(self, launch: LaunchSpec, channel: Provisioned, exchange: str) -> ChildHandle:
    assert isinstance(launch, SummonLaunchSpec)
    workspace_name = _workspace_name(channel.channel)
    self._peers.note_workspace(exchange, workspace_name)
    lowered = await asyncio.to_thread(
      _lower_summon,
      launch,
      workspace_name,
      self._container_runtime,
      self._artifacts,
    )
    return await self._docker.spawn(lowered, channel, exchange)


def _root_lifecycle(control: SummonControl, workspace: Workspace):
  """consume the root's own run lifecycle off the delivery tap. The root answers
  the session's host-anchored exchange, whose deliveries reach only the
  observers, with no target peer — the filter that keeps every child delivery
  out. A started progress publishes the root's trail (what its summon children
  are attributed to); the run's result is logged, a raised run loudly."""

  def _observe(source: Optional[Peer], target: Optional[Peer], message: Message) -> None:
    del source
    if target is not None:
      return
    if message.type == Tag.PROGRESS:
      trail_id = message.payload.get('trail_id')
      if trail_id is None:
        return
      log.info('root run started (trail %s)', trail_id)
      control.note_root_trail(trail_id)
      if isinstance(trail_id, str) and len(trail_id) > 0:
        trail_pointer.write(trail_pointer.session_pointer(workspace.path), trail_id)
      return
    if message.type == Tag.RESULT:
      detail = message.payload.get('detail')
      reason = detail.get('reason') if isinstance(detail, dict) else None
      if reason == 'raised':
        # a raised run's error is the abort reason — surface it
        log.warning('root run raised: %s', message.payload.get('error'))
        return
      log.info('root run ended: %s', message.payload.get('outcome'))

  return _observe


def _note_child_started(peers: Peers):
  """publish each summoned child's started trail id as its workspace's session
  trail pointer — what makes a failed child's surviving workspace resumable.
  Sees only child deliveries: a host-anchored delivery (the root's, no target
  peer) is filtered out, so no pointer is fabricated for a workspace that
  doesn't exist. The name comes from the peer registry — a spawned child's
  channel-named workspace or a manual child's claimed one — with the worker's
  channel-derived name covering a non-summon exchange."""

  def _observe(source: Optional[Peer], target: Optional[Peer], message: Message) -> None:
    if message.type != Tag.PROGRESS or source is None or target is None:
      return
    trail_id = message.payload.get('trail_id')
    if not isinstance(trail_id, str) or len(trail_id) == 0:
      return
    name = peers.workspace_for(message.request) if message.request is not None else None
    if name is None:
      name = _workspace_name(source)
    trail_pointer.write(trail_pointer.session_pointer(workspace_dir(name)), trail_id)

  return _observe


def broker_bind_hosts() -> list[str]:
  """every address the session's channels must answer on: loopback for the peers
  this process launches beside itself, plus the docker bridge gateway when that
  is an address of this host, for the ones it launches in containers."""
  gateway = bridge_gateway()
  if gateway is None or gateway == LOCAL_HOST:
    return [LOCAL_HOST]
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    try:
      probe.bind((gateway, 0))
    except OSError:  # a daemon in a VM: its gateway proxies the container to loopback
      log.verbose('docker bridge gateway %s is not an address of this host', gateway)
      return [LOCAL_HOST]
  return [LOCAL_HOST, gateway]


def run_root_via_broker(
  launch: LaunchSpec,
  *,
  workspace: Workspace,
  may_summon: Collection[str] = (),
  credential_scope: Collection[str] = (),
  container_runtime: ContainerRuntimeResolver,
) -> int:
  """run `launch` as the root peer of a broker on this host, supervise it on the
  broker loop until it exits, and return its exit code. The spawner is the composite over both ride launch modes plus the summon
  lowering, so any root — host process or container — can spawn docker children.
  The broker answers the reserved ping kind, so a session can verify its channel
  (`broker request ping '{}'`), the artifact kinds over the session store
  (`ride.artifacts`, which also collects the run of any job a kind starts), plus
  whatever kinds installed distributions
  contribute (`ride.kinds`), and consumes the root's own run lifecycle — the
  progress and result of the session's host-anchored exchange — into the host
  log. While an interactive root owns the terminal, host output goes to the
  workspace's host log instead of the shared TTY (see
  `ride.workspace.spawn._HostLogRedirect`); headless runs keep it on stderr.

  `workspace` is the workspace the root session runs in — its name is the root's
  identity in the summon audit, and its records carry the broker-published
  current-trail pointer written from the root's `started` lifecycle
  (`bro.monitor.trail_pointer`). `may_summon` names the bros the root session
  is authorized to summon — its effective outgoing allow-list (`ride/ride/summon_control.py`);
  defaults to deny-all. `credential_scope` names the secrets the root session
  was launched with, the bound on what its summons may grant a child; defaults
  to grant-nothing. `container_runtime` is the root's lazy or already-resolved
  image and bundle-volume identity, reused by every child. A summoned child follows
  its own bro's static seeds instead, resolved per request by the control. The summon handler is registered
  either way, so a denied summoner always gets a clean correlated error instead of
  a silent refuse; after the loop ends — cleanly or by an exception unwinding out
  of it — children the root's exit killed mid-flight are logged loudly."""
  targets = sorted(set(may_summon))
  if len(targets) > 0:
    log.info('session may summon: %s', ', '.join(targets))
  host_log = workspace.host_log
  docker_spawner = DockerSpawner(host_log=host_log)
  peers = Peers(workspace)
  artifacts = ArtifactStore(workspace, root_in_container=isinstance(launch, DockerLaunchSpec))
  spawner = CompositeSpawner(
    {
      DockerLaunchSpec: docker_spawner,
      ProcessLaunchSpec: ProcessSpawner(host_log=host_log),
      SummonLaunchSpec: SummonSpawner(docker_spawner, container_runtime, peers, artifacts),
    }
  )
  control = SummonControl(
    allow_list=may_summon,
    credential_scope=credential_scope,
    workspace=workspace,
    peers=peers,
    artifacts=artifacts,
    status_file=summon_status_file(workspace.name),
    audit_file=summon_dir() / f'{workspace.name}.jsonl',
  )
  artifact_control = ArtifactControl(artifacts, peers)
  facade = Broker(
    TcpServerTransport(broker_bind_hosts()), spawner, job_output=JobArtifacts(artifacts, peers)
  )
  facade.on(PING, ping_handler)
  facade.on(SUMMON, control.handle)
  facade.on(MINT, artifact_control.mint)
  facade.on(GET, artifact_control.get)
  for kind, handler in extension_kinds(KindContext(workspace.tree, artifact_control)).items():
    facade.on(kind, handler)
  facade.add_delivery_observer(control.observe_delivery)
  facade.add_delivery_observer(_note_child_started(peers))
  facade.add_delivery_observer(_root_lifecycle(control, workspace))
  try:
    with contextlib.closing(artifacts):
      return facade.run(launch)
  finally:
    control.log_killed_in_flight()
