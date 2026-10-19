"""The registered bro worker type and its summon lowering.

`SummonSpawner` lowers starts and joins off-loop, then delegates the resulting Docker, process, or exec launch to its workspace spawner.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol, cast

from bro.authority.patterns import Descriptor
from bro.authority.records import dump_request
from bro.authority.rules import Mission, Prepared
from bro.base import configs, credentials, log
from bro.quest import BRO
from bro.summon import (
  DEFAULT_TIMEOUT,
  PARTY_MEMBER_ENV,
  summoned_child_env,
)
from bro.worker_types import (
  Expect,
  LaunchDenied,
  LaunchRequest,
  Spawn,
  UnattributablePeer,
  WorkerType,
)
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT
from ride.workspace.metadata import Isolation

if TYPE_CHECKING:
  from bro.broker.brotocol import Talk
  from bro.broker.journal import Event, Record
  from bro.broker.spawn import ChildHandle
  from bro.broker.transport import Provisioned
  from ride.artifacts import ArtifactStore
  from ride.authority_reading import Reading
  from ride.peer_facts import PeerFacts
  from ride.repository import Repository
  from ride.runtime_bundle import RuntimeBundle
  from ride.session import ScopedLaunch, SessionSpec
  from ride.workspace.docker import ContainerRuntimeResolver
  from ride.workspace.spawn import (
    DockerLaunchSpec,
    DockerSpawner,
    ExecLaunchSpec,
    ExecSpawner,
    ProcessLaunchSpec,
    ProcessSpawner,
  )
  from ride.workspace.store import ScopedSecrets


def get_harness(name: str):
  from ride.harness import get_harness as resolve

  return resolve(name)


def default_hold(*, solo: bool, isolation: Isolation) -> str:
  from ride.flags import default_hold as resolve

  return resolve(solo=solo, isolation=isolation)


def resolve_head(*args):
  from bro.workspace.git import resolve_head as resolve

  return resolve(*args)


def resolve_ref(*args):
  from bro.workspace.git import resolve_ref as resolve

  return resolve(*args)


def human_git_identity_env(repository):
  from ride.identity import human_git_identity_env as resolve

  return resolve(repository)


def record_resume_spec(*args, **kwargs):
  from ride.session import record_resume_spec as lower

  return lower(*args, **kwargs)


def started_party_launch(*args, **kwargs):
  from ride.session import started_party_launch as lower

  return lower(*args, **kwargs)


def boxed_member_launch(*args, **kwargs):
  from ride.session import boxed_member_launch as lower

  return lower(*args, **kwargs)


def prepared_unboxed_session_launch(*args, **kwargs):
  from ride.session import prepared_unboxed_session_launch as lower

  return lower(*args, **kwargs)


def workspace_isolation(name: str) -> Isolation:
  from ride.workspace.model import Workspace

  return Workspace.open(name).isolation


class BroHost(Protocol):
  peers: Any
  reading: Reading
  workspace: Any
  journal: Any
  depth_cap: int
  summon_harness: str
  session_env: dict[str, str]
  summon_spawner: Any


@dataclass(frozen=True)
class SummonLaunchSpec:
  """an authorized summon as a launch description, cheap to build on the broker
  loop: the request fields plus the summoner's workspace name and tree, attributed
  by the control at request time.
  `SummonSpawner` lowers it to a concrete launch off-loop — the target-bro
  import, scoped-set computation, and any base-ref resolution are blocking work
  the broker loop should not carry.

  `passes` become the child's credential picks and grants.
  `grant`/`revoke` are the request's authority values for the recorded session spec.
  The control already resolved them into the child's own launch section and a request
  naming no `harness` into the control's summon harness.
  `share` names artifact refs the control already checked against the
  summoner's own reach; the lowering links them into the child's view.
  `env` is the party's `--env` additions, carried by the child's session like
  the root's."""

  target: str
  prompt: str
  parent: str
  parent_tree: Path
  summoner: Optional[dict[str, Any]]
  record: Mission
  harness: str
  repo: Optional[Repository | Path] = None
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH
  summon_harness: str = configs.DEFAULT_SUMMON_HARNESS
  into: Optional[str] = None
  hold: Optional[str] = None
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  passes: tuple[str, ...] = ()
  share: tuple[str, ...] = ()
  llm: Optional[str] = None
  party: Literal['start', 'join'] = 'start'
  isolation: Optional[Isolation] = Isolation.BOXED
  env: dict[str, str] = field(default_factory=dict)

  def __post_init__(self) -> None:
    if self.party not in ('start', 'join'):
      raise ValueError("summon party must be 'start' or 'join'")
    if self.party == 'start' and self.isolation is None:
      raise ValueError('a party start needs an isolation')
    if self.party == 'join' and self.isolation is not None:
      raise ValueError('a party join inherits its isolation')


def _workspace_name(channel: str) -> str:
  return f'broker-{channel}'


def _pass_scope_overrides(passes: Sequence[str]) -> tuple[list[str], list[str]]:
  credential_grants: list[str] = []
  for name in passes:
    kind, instance = credentials.parse_name(name)
    if instance is None:
      raise ValueError(f'passed credential {name!r} names no instance')
    credential_grants.append(kind)
  return list(passes), credential_grants


def _child_session_spec(
  launch: SummonLaunchSpec,
  workspace_name: str,
  runtime_reference: Optional[str],
  *,
  isolation: Optional[Isolation] = None,
) -> SessionSpec:
  """The summoned child's run as an unpinned solo `SessionSpec`, its `llm`
  settled over the host's per-bro default like a launch's own.

  A started party records its resume variant;
  a joined member uses the spec only to build its `do-ride` argv and environment."""
  from ride.authority_scope import checked_recipe
  from ride.repository import as_repository
  from ride.session import SessionSpec
  harness = get_harness(launch.harness)
  resolved_isolation = launch.isolation if isolation is None else isolation
  if resolved_isolation is None:
    raise ValueError('summoned session isolation is unresolved')
  repo = None if launch.repo is None else as_repository(launch.repo).identity
  spec = checked_recipe(
    launch.record.request, launch.record.descriptor, cast('Reading', launch.record.reading)
  )
  llm = launch.llm
  pass_picks, pass_grants = [], []
  return SessionSpec(
    name=workspace_name,
    repo=repo,
    harness=harness.name,
    workspace_pinned=False,
    isolation=resolved_isolation,
    drop=True,
    no_trails=False,
    hold=launch.hold
    if launch.hold is not None
    else default_hold(solo=True, isolation=resolved_isolation),
    cred=pass_picks,
    grant=[*pass_grants, *launch.grant],
    revoke=list(launch.revoke),
    llm=llm,
    resolved_llm=spec.dump(),
    authority_request=dump_request(launch.record.request, launch.record.descriptor),
    mission=launch.record,
    solo=True,
    resume=False,
    into=launch.into,
    bro=launch.target,
    prompt=launch.prompt,
    subject=launch.prompt,
    summon_depth=launch.summon_depth,
    summon_harness=launch.summon_harness,
    runtime_bundle=runtime_reference,
    env=dict(launch.env),
  )


def _child_launch_scope(
  launch: SummonLaunchSpec,
  spec: SessionSpec,
  repository: Optional[Repository],
) -> tuple[ScopedLaunch, ScopedSecrets]:
  from ride.authority_launch import credentials_for, hydrate
  from ride.session import ScopedLaunch

  scoped = credentials_for(launch.record)
  files, kinds = hydrate(scoped)
  return ScopedLaunch(
    scoped=scoped, store=files, hydrated_kinds=kinds, mission=launch.record
  ), scoped


def _lower_summon(
  launch: SummonLaunchSpec,
  workspace_name: str,
  runtime_bundle: RuntimeBundle,
  container_runtime: ContainerRuntimeResolver,
  artifacts: ArtifactStore,
) -> DockerLaunchSpec | ProcessLaunchSpec:
  """Prepare a summoned started party in its requested isolation."""
  from ride.artifacts import view_mount
  from ride.repository import as_repository
  from ride.root import ProcessLaunch
  from ride.workspace.model import Workspace
  from ride.workspace.spawn import DockerLaunchSpec, ProcessLaunchSpec
  from ride.workspace.store import log_scoped_secrets

  if launch.party != 'start' or launch.isolation is None:
    raise ValueError('started-party lowering needs a start request with an isolation')
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
    base_ref = resolve_head(repo.git_dir, launch.parent_tree)
    if base_ref is None:
      raise ValueError(f"cannot read the summoner's HEAD at {launch.parent_tree}")
  spec = _child_session_spec(launch, workspace_name, runtime_bundle.recorded_reference)
  launch_scope, scoped = _child_launch_scope(launch, spec, repo)
  if launch.isolation is Isolation.BOXED:
    container_runtime.resolve()
  workspace = Workspace.ensure(workspace_name, repo, launch.isolation, throwaway=True)
  record_resume_spec(workspace, spec)
  mounts: tuple[str, ...] = ()
  temporary_store: Optional[Path] = None
  if launch.isolation is Isolation.BOXED:
    artifacts.view(workspace_name)
    mounts = (
      view_mount(
        artifacts.ride,
        workspace_name,
        PurePosixPath(CONTAINER_ARTIFACTS_ROOT),
      ),
    )
  else:
    temporary_store = Path(tempfile.mkdtemp(prefix=f'ride-{workspace_name}-store-'))
  with contextlib.ExitStack() as cleanup:
    if temporary_store is not None:
      cleanup.callback(shutil.rmtree, temporary_store)
    artifacts.share(launch.share, to=workspace_name, by=launch.parent)
    prepared = started_party_launch(
      spec,
      workspace,
      repo,
      base_ref,
      launch_scope,
      human_env=human_git_identity_env(repo),
      runtime_bundle=runtime_bundle,
      container_runtime=container_runtime,
      env=summoned_child_env(launch.summoner),
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


def _joined_ride_command(launch: SummonLaunchSpec) -> str:
  parts = ['summon', '--join']
  if launch.hold is not None:
    parts.extend(['--hold', launch.hold])
  for value in launch.grant:
    parts.extend(['--grant', value])
  for value in launch.revoke:
    parts.extend(['--revoke', value])
  for value in launch.passes:
    parts.extend(['--pass', value])
  for value in launch.share:
    parts.extend(['--share', value])
  if launch.llm is not None:
    parts.extend(['--llm', launch.llm])
  parts.extend(['--harness', launch.harness, launch.target, launch.prompt])
  return ' '.join(parts)


def _lower_join(
  launch: SummonLaunchSpec,
  member: str,
  runtime_bundle: RuntimeBundle,
  artifacts: ArtifactStore,
) -> ProcessLaunchSpec | ExecLaunchSpec:
  """Prepare a member beside its summoner: a process in an unboxed party's
  tree, an exec into a boxed party's running container."""
  from bro.monitor import party_member_dir
  from ride.repository import as_repository
  from ride.workspace.model import Workspace
  from ride.workspace.spawn import ExecLaunchSpec, ProcessLaunchSpec
  from ride.workspace.store import log_scoped_secrets

  workspace = Workspace.open(launch.parent)
  repository = None if launch.repo is None else as_repository(launch.repo)
  spec = _child_session_spec(
    launch,
    workspace.name,
    runtime_bundle.recorded_reference,
    isolation=workspace.isolation,
  )
  launch_scope, scoped = _child_launch_scope(launch, spec, repository)
  records = party_member_dir(workspace.path, member)
  records.mkdir(parents=True)
  ride_command = _joined_ride_command(launch)
  member_env = {
    'RIDE_COMMAND': ride_command,
    PARTY_MEMBER_ENV: member,
    **summoned_child_env(launch.summoner),
  }

  if workspace.isolation is Isolation.BOXED:
    prepared_exec = boxed_member_launch(
      spec,
      workspace,
      member,
      launch_scope,
      human_env=human_git_identity_env(repository),
      runtime_bundle=runtime_bundle,
      env=member_env,
    )
    artifacts.share(launch.share, to=workspace.name, by=launch.parent)
    log_scoped_secrets(f'summoned {launch.target}', scoped.required, scoped.optional)
    return ExecLaunchSpec(
      launch=prepared_exec,
      party_workspace=workspace.name,
      records_directory=str(records),
    )

  temporary_store = Path(tempfile.mkdtemp(prefix=f'ride-{member}-store-'))
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(shutil.rmtree, temporary_store)
    artifacts.share(launch.share, to=workspace.name, by=launch.parent)
    prepared = prepared_unboxed_session_launch(
      spec,
      workspace,
      launch_scope,
      human_env=human_git_identity_env(repository),
      runtime_bundle=runtime_bundle,
      env=member_env,
      credential_directory=temporary_store / 'store',
      install_directory=temporary_store / 'environment',
      records_directory=records,
    )
    log_scoped_secrets(f'summoned {launch.target}', scoped.required, scoped.optional)
    lowered = ProcessLaunchSpec(
      command=prepared.command,
      cwd=prepared.cwd,
      env=prepared.env,
      interactive=False,
      capture_output=True,
      party_workspace=workspace.name,
      records_directory=str(records),
      cleanup_directory=str(temporary_store),
    )
    cleanup.pop_all()
    return lowered


class SummonSpawner:
  """Lower a party start or join off-loop and dispatch its concrete launch."""

  def __init__(
    self,
    docker: DockerSpawner,
    process: ProcessSpawner,
    exec_spawner: ExecSpawner,
    runtime_bundle: RuntimeBundle,
    container_runtime: ContainerRuntimeResolver,
    facts: PeerFacts,
    artifacts: ArtifactStore,
  ):
    self._docker = docker
    self._process = process
    self._exec = exec_spawner
    self._runtime_bundle = runtime_bundle
    self._container_runtime = container_runtime
    self._facts = facts
    self._artifacts = artifacts

  async def spawn(self, launch: Any, channel: Provisioned, mission: str, talk: Talk) -> ChildHandle:
    from ride.workspace.spawn import DockerLaunchSpec, ExecLaunchSpec

    assert isinstance(launch, SummonLaunchSpec)
    child_name = _workspace_name(channel.channel)
    if launch.party == 'join':
      self._facts.note_member(
        mission,
        launch.parent,
        child_name,
        artifact_view=(
          PurePosixPath(CONTAINER_ARTIFACTS_ROOT)
          if workspace_isolation(launch.parent) is Isolation.BOXED
          else None
        ),
      )
      lowered = await asyncio.to_thread(
        _lower_join,
        launch,
        child_name,
        self._runtime_bundle,
        self._artifacts,
      )
    else:
      self._facts.note_workspace(
        mission,
        child_name,
        artifact_view=(
          PurePosixPath(CONTAINER_ARTIFACTS_ROOT) if launch.isolation is Isolation.BOXED else None
        ),
      )
      lowered = await asyncio.to_thread(
        _lower_summon,
        launch,
        child_name,
        self._runtime_bundle,
        self._container_runtime,
        self._artifacts,
      )
    if isinstance(lowered, DockerLaunchSpec):
      return await self._docker.spawn(lowered, channel, mission, talk)
    if isinstance(lowered, ExecLaunchSpec):
      return await self._exec.spawn(lowered, channel, mission, talk)
    return await self._process.spawn(lowered, channel, mission, talk)


@dataclass(frozen=True)
class Placement:
  party: Literal['start', 'join']
  isolation: Isolation | None


@dataclass(frozen=True)
class BroFacts:
  bro: str
  placement: Placement = Placement('start', Isolation.BOXED)


class BroType(WorkerType):
  host: BroHost
  name = BRO
  default_timeout = DEFAULT_TIMEOUT
  widens_talk = True
  manual = True

  @classmethod
  def authority_declaration(cls, credential_kinds: frozenset[str]):
    from ride.authority_scope import declaration

    return declaration(credential_kinds)

  @classmethod
  def option_values(cls, fields: Mapping[str, Any], reading: object):
    from ride.authority_scope import option_values

    return option_values(fields, reading)

  def __init__(self, host: BroHost):
    super().__init__(cast(Any, host))
    if (
      not isinstance(host.depth_cap, int) or isinstance(host.depth_cap, bool) or host.depth_cap <= 0
    ):
      raise ValueError('summon depth cap must be a positive integer')

  def talk(self, request: LaunchRequest) -> Talk:
    return request.requested_talk

  def launch(self, request: LaunchRequest) -> Spawn | Expect:
    args = request.args
    record = request.record
    if record is None:
      raise ValueError('bro start requires an admitted record')
    values = record.request.values if isinstance(record, Prepared) else record.descriptor.values
    target = cast(str, values['target'])
    prompt = args.get('prompt')
    if not isinstance(prompt, str) or not prompt:
      raise LaunchDenied('content check: bro.prompt must be a non-empty string')
    attribution = self.host.peers.attribution_for_mission(self.host.journal, request.owner.mission)
    summoned_by = self._summoned_by(attribution, args)
    if isinstance(record, Prepared):
      return Expect(
        {
          'target': target,
          'prompt': prompt,
          'request': dump_request(record.request, Descriptor('bro', {})),
          'summoner': summoned_by,
          'repo': self.host.workspace.metadata.repo,
          'into': args.get('into'),
          'root': self.host.workspace.name,
        },
        BroFacts(bro=target, placement=Placement('start', None)),
      )
    placement_value = values['placement']
    placement = (
      Placement('join', None)
      if placement_value == 'join'
      else Placement('start', Isolation(cast(str, placement_value)))
    )
    return Spawn(
      SummonLaunchSpec(
        target=target,
        prompt=prompt,
        parent=request.owner.workspace,
        parent_tree=request.owner.tree,
        repo=self.host.workspace.repository,
        summoner=summoned_by,
        record=record,
        harness=cast(str, values['harness']),
        summon_depth=self.host.reading.summon_depth,
        summon_harness=self.host.reading.summon_harness,
        into=args.get('into'),
        hold=cast(str, values['hold']),
        share=request.share,
        party=placement.party,
        isolation=placement.isolation,
        env=dict(self.host.session_env),
      ),
      self.host.summon_spawner,
      BroFacts(bro=target, placement=placement),
    )

  @staticmethod
  def _summoned_by(attribution: dict[str, str], args: dict[str, Any]) -> dict[str, Any] | None:
    trail_id = attribution.get('trail_id')
    if trail_id is None:
      return None
    value: dict[str, Any] = {'trail_id': trail_id}
    step_id = args.get('step_id')
    if step_id is not None:
      value['step_id'] = step_id
      if args.get('index') is not None:
        value['index'] = args['index']
    return value

  def audit_fields(self, extension: Any) -> Mapping[str, Any]:
    if not isinstance(extension, BroFacts):
      raise TypeError('bro launch facts are missing')
    return {
      'placement': {
        'party': extension.placement.party,
        'isolation': (
          None if extension.placement.isolation is None else extension.placement.isolation.value
        ),
      },
      'target': extension.bro,
    }

  def subscribers(self) -> Sequence[Callable[[Event, Record], None]]:
    return (self._observe_journal,)

  def _observe_journal(self, event: Event, record: Record) -> None:
    if record.kind == 'root':
      if event.transition == 'trail':
        log.info('root run trail %s', record.trail_id)
      elif event.transition == 'ended':
        reason = event.payload.get('reason')
        if reason == 'raised':
          log.warning('root run raised: %s', record.result)
        else:
          log.info('root run ended: %s', event.payload.get('outcome'))
      return
    if record.kind != 'launch' or record.type != self.name:
      return
    try:
      facts = self.host.peers.for_mission(event.mission).extension
    except UnattributablePeer:
      return
    if not isinstance(facts, BroFacts):
      return
    if event.transition == 'accepted':
      action = (
        'expecting a manual launch'
        if self.host.peers.for_mission(event.mission).expected
        else 'spawning'
      )
      log.info(
        'summon: %s %s %s (quest %s)',
        self.host.workspace.name,
        action,
        facts.bro,
        event.mission,
      )
    elif event.transition == 'trail':
      log.info('summon: %s trail %s', facts.bro, record.trail_id)
    elif event.transition == 'ended':
      outcome = str(event.payload.get('outcome'))
      reason = event.payload.get('reason')
      if outcome == 'failed' and reason is not None:
        outcome = f'{outcome}:{reason}'
      log.info('summon: %s ended: %s (trail %s)', facts.bro, outcome, record.trail_id)


@dataclass(frozen=True)
class PendingBro:
  launch: Any
  target: str
  prompt: str
  request: dict[str, Any]
  root: str
  summoner: dict[str, Any] | None
  repo: str | None
  into: str | None

  @property
  def token(self) -> str:
    return self.launch.token

  @property
  def runtime(self) -> str:
    return self.launch.runtime

  @property
  def talk(self) -> tuple[str, ...]:
    return self.launch.talk

  @property
  def owner_tree(self) -> str:
    return self.launch.owner_tree

  @property
  def env(self) -> dict[str, str]:
    return self.launch.env

  def address(self, host: str | None = None) -> str:
    return self.launch.address(host)


def pending_bro(launch: Any) -> PendingBro:
  if launch.type != BRO:
    raise ValueError(f'the launch token names worker type {launch.type!r}, not bro')
  data = launch.extension
  expected = {'target', 'prompt', 'request', 'root', 'summoner', 'repo', 'into'}
  if set(data) != expected:
    raise ValueError('pending bro launch carries an invalid extension shape')
  for key in ('target', 'prompt', 'root'):
    if not isinstance(data[key], str) or not data[key]:
      raise ValueError(f'pending bro launch carries invalid {key}')
  return PendingBro(
    launch,
    data['target'],
    data['prompt'],
    data['request'],
    data['root'],
    data['summoner'],
    data['repo'],
    data['into'],
  )
