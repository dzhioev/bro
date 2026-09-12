"""Summon lowering and broker-root composition over workspace spawners.

`SummonSpawner` resolves the requested base ref off-loop and prepares the child
through the same isolation-parameterized started-party launcher a root uses.
It records the channel-named workspace as throwaway and delegates the resulting
Docker or process launch to the matching spawner.

`run_root_via_broker` composes both launch modes and summon lowering under one
broker, then supervises the root until exit.
"""

import asyncio
import contextlib
import shutil
import socket
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from bro.artifact import GET, MINT
from bro.base import configs, log
from bro.base.scope import DEFAULT_PERMITS
from bro.broker.dispatcher import PING, Broker, ping_handler
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.kinds import KindContext
from bro.summon import SUMMON, summoned_child_env
from bro.workspace.git import resolve_head, resolve_ref
from bro.workspace.paths import summon_dir, workspace_tree
from ride.artifacts import ArtifactControl, ArtifactStore, JobArtifacts, view_mount
from ride.flags import default_hold
from ride.harness import get_harness
from ride.identity import human_git_identity_env
from ride.kinds import extension_kinds
from ride.peer_facts import PeerFact, PeerFacts
from ride.repository import Repository, as_repository
from ride.root import ProcessLaunch
from ride.runtime_bundle import RuntimeBundle
from ride.scope import preflight_scoped_launch, scoped_secrets
from ride.session import ScopedLaunch, SessionSpec, record_resume_spec, started_party_launch
from ride.summon_control import SummonControl
from ride.workspace.docker import ContainerRuntimeResolver, bridge_gateway
from ride.workspace.metadata import Isolation
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
  against — and a request naming no `harness` into the control's summon harness.
  `share` names artifact refs the control already checked against the
  summoner's own reach; the lowering links them into the child's view."""

  target: str
  prompt: str
  parent: str
  summoner: Optional[dict[str, Any]]
  may_summon: tuple[str, ...]
  harness: str
  permits: tuple[str, ...] = tuple(DEFAULT_PERMITS)
  repo: Optional[Repository | Path] = None
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH
  summon_harness: str = configs.DEFAULT_SUMMON_HARNESS
  into: Optional[str] = None
  hold: Optional[str] = None
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  share: tuple[str, ...] = ()
  llm: Optional[str] = None
  isolation: Isolation = Isolation.BOXED


def _workspace_name(channel: str) -> str:
  return f'broker-{channel}'


def _child_session_spec(launch: SummonLaunchSpec, workspace_name: str) -> SessionSpec:
  """the summoned child's run as a `SessionSpec`: an unpinned solo session
  of the target bro in the requested isolation — only the request's
  `timeout` maps to no spec field (it is the spawner's wait timer, not part of
  the run). Recorded as the workspace's resume record and the source of the
  child's `do-ride` argv, so what `ride resume` relaunches is what ran."""
  harness = get_harness(launch.harness)
  return SessionSpec(
    name=workspace_name,
    repo=None if launch.repo is None else as_repository(launch.repo).identity,
    harness=harness.name,
    workspace_pinned=False,
    isolation=launch.isolation,
    drop=True,
    no_trails=False,
    hold=launch.hold
    if launch.hold is not None
    else default_hold(solo=True, isolation=Isolation.BOXED),
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
    summon_depth=launch.summon_depth,
    summon_harness=launch.summon_harness,
  )


def _lower_summon(
  launch: SummonLaunchSpec,
  workspace_name: str,
  runtime_bundle: RuntimeBundle,
  container_runtime: ContainerRuntimeResolver,
  artifacts: ArtifactStore,
) -> DockerLaunchSpec | ProcessLaunchSpec:
  """Prepare a summoned started party in its requested isolation."""
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
  scoped = scoped_secrets(
    launch.target,
    harness.scope_recipe(spec.harness_options),
    attachment=None if repo is None else repo.identity,
    attachment_repository=repo,
    grant=spec.grant,
    revoke=spec.revoke,
    llm_spec=spec.llm_spec,
  )
  _, _, store = preflight_scoped_launch(
    scoped,
    spec.bro,
    attachment=spec.repo,
    attachment_repository=repo,
    grant=spec.grant,
    revoke=spec.revoke,
  )
  launch_scope = ScopedLaunch(
    scoped=scoped,
    may_summon=set(launch.may_summon),
    permits=set(launch.permits),
    store=store,
    hydrated_kinds=store.kinds,
  )
  if launch.isolation is Isolation.BOXED:
    container_runtime.resolve()
  else:
    runtime_bundle.materialize_host()
  workspace = Workspace.ensure(workspace_name, repo, launch.isolation, throwaway=True)
  record_resume_spec(workspace, spec)
  mounts: tuple[str, ...] = ()
  temporary_store: Optional[Path] = None
  if launch.isolation is Isolation.BOXED:
    artifacts.view(workspace_name)
    mounts = (view_mount(artifacts.ride, workspace_name),)
  else:
    temporary_store = Path(tempfile.mkdtemp(prefix=f'ride-{workspace_name}-store-'))
  artifacts.share(launch.share, to=workspace_name, by=launch.parent)
  with contextlib.ExitStack() as cleanup:
    if temporary_store is not None:
      cleanup.callback(shutil.rmtree, temporary_store)
    prepared = started_party_launch(
      spec,
      workspace,
      repo,
      base_ref,
      launch_scope,
      human_env=human_git_identity_env(repo),
      runtime_bundle=runtime_bundle,
      container_runtime=container_runtime,
      forward_env=False,
      env={
        'RIDE_COMMAND': ' '.join(spec.to_command_argv()),
        **summoned_child_env(launch.may_summon, launch.permits, launch.summoner),
      },
      mounts=mounts,
      credential_directory=(
        workspace.path / 'credentials' if temporary_store is None else temporary_store / 'store'
      ),
      install_directory=(
        workspace.path / 'environment'
        if temporary_store is None
        else temporary_store / 'environment'
      ),
    )
    log_scoped_secrets(f'summoned {launch.target}', scoped.required, scoped.optional)
    if not isinstance(prepared, ProcessLaunch):
      return DockerLaunchSpec(prepared)
    if temporary_store is None:
      raise ValueError('an unboxed summon has no private credential store')
    lowered = ProcessLaunchSpec(
      command=prepared.command,
      cwd=prepared.cwd,
      env=prepared.env,
      interactive=False,
      capture_output=True,
      workspace=workspace.name,
      cleanup_directory=str(temporary_store),
    )
    cleanup.pop_all()
    return lowered


class SummonSpawner(Spawner):
  """Lower a summon to the requested started-party isolation off-loop."""

  def __init__(
    self,
    docker: DockerSpawner,
    process: ProcessSpawner,
    runtime_bundle: RuntimeBundle,
    container_runtime: ContainerRuntimeResolver,
    facts: PeerFacts,
    artifacts: ArtifactStore,
  ):
    self._docker = docker
    self._process = process
    self._runtime_bundle = runtime_bundle
    self._container_runtime = container_runtime
    self._facts = facts
    self._artifacts = artifacts

  async def spawn(self, launch: LaunchSpec, channel: Provisioned, quest: str) -> ChildHandle:
    assert isinstance(launch, SummonLaunchSpec)
    workspace_name = _workspace_name(channel.channel)
    self._facts.note_workspace(quest, workspace_name)
    lowered = await asyncio.to_thread(
      _lower_summon,
      launch,
      workspace_name,
      self._runtime_bundle,
      self._container_runtime,
      self._artifacts,
    )
    if isinstance(lowered, DockerLaunchSpec):
      return await self._docker.spawn(lowered, channel, quest)
    return await self._process.spawn(lowered, channel, quest)


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
  bro: str,
  may_summon: Collection[str] = (),
  permits: Collection[str] = DEFAULT_PERMITS,
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH,
  summon_harness: str = configs.DEFAULT_SUMMON_HARNESS,
  credential_scope: ScopedSecrets,
  container_runtime: ContainerRuntimeResolver,
  runtime_bundle: RuntimeBundle,
) -> int:
  """run `launch` as the root peer of a broker on this host, supervise it on the
  broker loop until it exits, and return its exit code. The spawner composes both
  workspace isolations plus summon lowering, so either root can start either kind
  of child.
  The broker answers the reserved ping kind, so a session can verify its channel
  (`broker request ping '{}'`), the artifact kinds over the ride store
  (`ride.artifacts`, which also collects the run of any job a kind starts), plus
  whatever kinds installed distributions
  contribute (`ride.kinds`), and projects the journal into the summon audit and
  manual-token cleanup. While an interactive root owns the terminal, host output goes to the
  workspace's host log instead of the shared TTY (see
  `ride.workspace.spawn._HostLogRedirect`); headless runs keep it on stderr.

  `workspace` is the workspace the root session runs in — its name identifies the
  ride in the summon audit. `may_summon` names the bros the root session
  is authorized to summon — its effective outgoing allow-list (`ride/ride/summon_control.py`);
  defaults to deny-all. `credential_scope` carries the kinds the root session
  was launched with and their selection, the bound on what its summons may grant
  a child. `container_runtime` is the root's lazy or already-resolved
  image and bundle-volume identity for boxed children;
  `runtime_bundle` is the matching host materialization for unboxed children.
  A summoned child follows
  its own bro's static seeds instead, resolved per request by the control. The summon handler is registered
  either way, so a denied summoner gets a correlated error and an ordinary
  journal denial event.
  `summon_depth` is the deepest child generation that handler authorizes, with the
  root itself at depth 0, and `summon_harness` the harness it runs a child under
  when the request names none."""
  targets = sorted(set(may_summon))
  if len(targets) > 0:
    log.info('session may summon: %s', ', '.join(targets))
  host_log = workspace.host_log
  docker_spawner = DockerSpawner(host_log=host_log)
  process_spawner = ProcessSpawner(host_log=host_log)
  root_scope = ScopedSecrets(
    required=set(credential_scope.required),
    optional=set(credential_scope.optional),
    selection=dict(credential_scope.selection),
  )
  facts = PeerFacts(
    PeerFact(
      workspace=workspace.name,
      bro=bro,
      allow_list=frozenset(may_summon),
      permits=frozenset(permits),
      credential_scope=root_scope,
    ),
    root_tree=workspace.tree,
    root_path=workspace.path,
  )
  artifacts = ArtifactStore(workspace, root_boxed=isinstance(launch, DockerLaunchSpec))
  spawner = CompositeSpawner(
    {
      DockerLaunchSpec: docker_spawner,
      ProcessLaunchSpec: process_spawner,
      SummonLaunchSpec: SummonSpawner(
        docker_spawner,
        process_spawner,
        runtime_bundle,
        container_runtime,
        facts,
        artifacts,
      ),
    }
  )
  artifact_control = ArtifactControl(artifacts, facts)
  facade = Broker(
    TcpServerTransport(broker_bind_hosts()), spawner, job_output=JobArtifacts(artifacts, facts)
  )
  control = SummonControl(
    workspace=workspace,
    facts=facts,
    artifacts=artifacts,
    journal=facade.journal,
    audit_file=summon_dir() / f'{workspace.name}.jsonl',
    depth_cap=summon_depth,
    summon_harness=summon_harness,
  )
  facade.on(PING, ping_handler)
  facade.on(SUMMON, control.handle)
  facade.on(MINT, artifact_control.mint)
  facade.on(GET, artifact_control.get)
  kind_context = KindContext(
    workspace.tree,
    artifact_control,
    frozenset(credential_scope.required | credential_scope.optional),
  )
  for kind, handler in extension_kinds(kind_context).items():
    facade.on(kind, handler)
  facade.subscribe(facts.observe_journal)
  facade.subscribe(control.observe_journal)
  facade.subscribe(control.audit_event)
  with contextlib.closing(artifacts):
    return facade.run(launch)
