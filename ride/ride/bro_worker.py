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

from bro.base import configs, credentials, log
from bro.base.scope import ScopeLayer, apply_idempotent, split_scope_overrides
from bro.quest import BRO
from bro.summon import (
  DEFAULT_TIMEOUT,
  PARTY_JOIN,
  PARTY_MEMBER_ENV,
  PARTY_PERMIT_LEAVES,
  PARTY_START_BOXED,
  PARTY_START_UNBOXED,
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


def bind_launch_llm(*args):
  from ride.scope import bind_launch_llm as resolve

  return resolve(*args)


def preflight_scoped_launch(*args, **kwargs):
  from ride.scope import preflight_scoped_launch as resolve

  return resolve(*args, **kwargs)


def scoped_secrets(*args, **kwargs):
  from ride.scope import scoped_secrets as resolve

  return resolve(*args, **kwargs)


def launch_llm_spec(*args, **kwargs):
  from ride.scope import launch_llm_spec as resolve

  return resolve(*args, **kwargs)


def configured_scope_layers(*args, **kwargs):
  from ride.scope import configured_scope_layers as resolve

  return resolve(*args, **kwargs)


def effective_permits(*args, **kwargs):
  from ride.scope import effective_permits as resolve

  return resolve(*args, **kwargs)


def _harness_names() -> tuple[str, ...]:
  from ride.harness import HARNESS_NAMES

  return HARNESS_NAMES


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

  `grant`/`revoke` are the request's authority values for the recorded session spec.
  The control already resolved the `@bro` halves into `may_summon`, the child's own
  effective allow-list — never the summoner's, which the child is not authorized
  against — and a request naming no `harness` into the control's summon harness.
  `share` names artifact refs the control already checked against the
  summoner's own reach; the lowering links them into the child's view.
  `env` is the party's `--env` additions, carried by the child's session like
  the root's."""

  target: str
  prompt: str
  parent: str
  parent_tree: Path
  summoner: Optional[dict[str, Any]]
  may_summon: tuple[str, ...]
  harness: str
  permits: tuple[str, ...] = (PARTY_START_BOXED,)
  repo: Optional[Repository | Path] = None
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH
  summon_harness: str = configs.DEFAULT_SUMMON_HARNESS
  into: Optional[str] = None
  hold: Optional[str] = None
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
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
  from ride.repository import as_repository
  from ride.session import SessionSpec

  harness = get_harness(launch.harness)
  resolved_isolation = launch.isolation if isolation is None else isolation
  if resolved_isolation is None:
    raise ValueError('summoned session isolation is unresolved')
  repo = None if launch.repo is None else as_repository(launch.repo).identity
  llm = bind_launch_llm(repo, launch.target, launch.llm)
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
    cred=[],
    grant=list(launch.grant),
    revoke=list(launch.revoke),
    llm=llm,
    resolved_llm=harness.resolve_llm(llm, launch.target).dump(),
    solo=True,
    resume=False,
    into=launch.into,
    bro=launch.target,
    prompt=launch.prompt,
    subject=launch.prompt,
    arguments=[],
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
  from ride.session import ScopedLaunch

  harness = get_harness(spec.harness)
  scoped = scoped_secrets(
    launch.target,
    harness.scope_recipe(),
    attachment=None if repository is None else repository.identity,
    attachment_repository=repository,
    cred=spec.cred,
    llm_spec=spec.llm_spec,
  )
  auth_error = harness.preflight_auth(spec, scoped)
  if auth_error is not None:
    raise ValueError(auth_error)
  _, _, store = preflight_scoped_launch(
    scoped,
    spec.bro,
    attachment=spec.repo,
    attachment_repository=repository,
    grant=spec.grant,
    revoke=spec.revoke,
  )
  return (
    ScopedLaunch(
      scoped=scoped,
      may_summon=set(launch.may_summon),
      permits=set(launch.permits),
      store=store,
      hydrated_kinds=store.kinds,
    ),
    scoped,
  )


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
      env=summoned_child_env(launch.may_summon, launch.permits, launch.summoner),
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
    **summoned_child_env(launch.may_summon, launch.permits, launch.summoner),
  }

  if workspace.isolation is Isolation.BOXED:
    prepared_exec = boxed_member_launch(
      spec,
      workspace,
      member,
      scoped,
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
  allow_list: frozenset[str]
  placement: Placement = Placement('start', Isolation.BOXED)


_BRO_ARGUMENTS = frozenset(
  {
    'target',
    'prompt',
    'into',
    'hold',
    'step_id',
    'index',
    'grant',
    'revoke',
    'llm',
    'harness',
    'party',
    'isolation',
  }
)
_MANUAL_LAUNCH_OWNED = ('hold', 'llm', 'harness', 'party', 'isolation')
_DEFAULT_TALK = frozenset({'worker.say'})


def summon_allow_list(
  bro_name: str,
  *,
  grant: list[str],
  revoke: list[str],
  layers: Sequence[ScopeLayer] = (),
) -> set[str]:
  from bro.registry import create_bro, known_names

  allow_list = set(create_bro(bro_name)._may_summon)
  configured_names: set[str] = set()
  for layer in layers:
    layer_grant = split_scope_overrides(layer.grant)[1]
    layer_revoke = split_scope_overrides(layer.revoke)[1]
    configured_names.update(layer_grant)
    configured_names.update(layer_revoke)
    allow_list = apply_idempotent(allow_list, grant=layer_grant, revoke=layer_revoke)
  unknown = sorted((allow_list | configured_names | set(grant) | set(revoke)) - known_names())
  if unknown:
    raise ValueError(f'unknown summon target(s): {", ".join(unknown)}; not in the bro registry')
  return credentials.apply_grant_revoke(
    allow_list, grant=grant, revoke=revoke, subject='summon allow-list'
  )


def _validate_bro_arguments(args: dict[str, Any], *, manual: bool) -> None:
  from bro.llm.providers import LLMSelectionError, parse as parse_llm
  from bro.mcp import HOLDS

  unknown = sorted(set(args) - _BRO_ARGUMENTS)
  if unknown:
    raise LaunchDenied(f'unknown bro launch field(s): {", ".join(unknown)}')
  for key in ('target', 'prompt'):
    value = args.get(key)
    if not isinstance(value, str) or value == '':
      raise LaunchDenied(f'bro launch needs a non-empty string {key!r}')
  into = args.get('into')
  if into is not None and (not isinstance(into, str) or into == ''):
    raise LaunchDenied("bro launch 'into' must be a non-empty git ref")
  hold = args.get('hold')
  if hold is not None and hold not in HOLDS:
    raise LaunchDenied(f"bro launch 'hold' must be one of {', '.join(HOLDS)}")
  step_id = args.get('step_id')
  if step_id is not None and (
    not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0
  ):
    raise LaunchDenied("bro launch 'step_id' must be a non-negative int")
  index = args.get('index')
  if index is not None and (
    step_id is None or not isinstance(index, int) or isinstance(index, bool) or index < 0
  ):
    raise LaunchDenied("bro launch 'index' requires step_id and must be a non-negative int")
  for key in ('grant', 'revoke'):
    if key in args and (
      not isinstance(args[key], list)
      or not all(isinstance(value, str) and value for value in args[key])
    ):
      raise LaunchDenied(f'bro launch {key!r} must be a list of non-empty names')
  llm = args.get('llm')
  if llm is not None:
    if not isinstance(llm, str):
      raise LaunchDenied("bro launch 'llm' must be a string")
    try:
      parse_llm(llm)
    except LLMSelectionError as error:
      raise LaunchDenied(f"bro launch 'llm': {error}") from error
  harness = args.get('harness')
  harness_names = _harness_names()
  if harness is not None and harness not in harness_names:
    raise LaunchDenied(f"bro launch 'harness' must be one of {', '.join(harness_names)}")
  party = args.get('party')
  if party is not None and party not in ('start', 'join'):
    raise LaunchDenied("bro launch 'party' must be 'start' or 'join'")
  isolation = args.get('isolation')
  if isolation is not None and isolation not in {value.value for value in Isolation}:
    raise LaunchDenied("bro launch 'isolation' must be 'boxed' or 'unboxed'")
  if party == 'join':
    refused = [key for key in ('isolation', 'into') if key in args]
    if manual:
      refused.append('manual')
    if refused:
      raise LaunchDenied(f"a party join shares the owner's tree; drop {', '.join(refused)}")
  if manual:
    refused = sorted(key for key in _MANUAL_LAUNCH_OWNED if key in args)
    if refused:
      raise LaunchDenied(f'a manual bro launch owns {", ".join(refused)}; drop the field(s)')


def _placement(
  permits: set[str], *, party: str | None, isolation: str | None, manual: bool
) -> Placement:
  starts = {PARTY_START_BOXED, PARTY_START_UNBOXED}
  held = ', '.join(f':{permit}' for permit in sorted(permits)) or '(none)'
  if manual:
    if permits.isdisjoint(starts):
      raise LaunchDenied(f'a manual summon needs a party start permit; permits held: {held}')
    return Placement('start', None)
  if party == 'join':
    if PARTY_JOIN not in permits:
      raise LaunchDenied(f'joining a party needs :{PARTY_JOIN}; permits held: {held}')
    return Placement('join', None)
  if isolation is None:
    if PARTY_START_BOXED in permits:
      return Placement('start', Isolation.BOXED)
    if PARTY_START_UNBOXED in permits:
      return Placement('start', Isolation.UNBOXED)
    raise LaunchDenied(f'an unmarked summon needs a party start permit; permits held: {held}')
  resolved = Isolation(isolation)
  required = PARTY_START_BOXED if resolved is Isolation.BOXED else PARTY_START_UNBOXED
  if required not in permits:
    raise LaunchDenied(f'starting a {isolation} party needs :{required}; permits held: {held}')
  return Placement('start', resolved)


class BroType(WorkerType):
  host: BroHost
  name = BRO
  permits = PARTY_PERMIT_LEAVES
  default_timeout = DEFAULT_TIMEOUT
  widens_talk = True
  manual = True

  def __init__(self, host: BroHost):
    super().__init__(cast(Any, host))
    if (
      not isinstance(host.depth_cap, int) or isinstance(host.depth_cap, bool) or host.depth_cap <= 0
    ):
      raise ValueError('summon depth cap must be a positive integer')
    harness_names = _harness_names()
    if host.summon_harness not in harness_names:
      raise ValueError(f'summon harness must be one of {", ".join(harness_names)}')

  def talk(self, request: LaunchRequest) -> Talk:
    return cast('Talk', _DEFAULT_TALK | request.requested_talk)

  def launch(self, request: LaunchRequest) -> Spawn | Expect:
    args = request.args
    _validate_bro_arguments(args, manual=request.manual)
    if request.owner.type != self.name or not isinstance(request.owner.extension, BroFacts):
      raise LaunchDenied('a worker of another type cannot summon a bro')
    owner = request.owner.extension
    if request.owner.depth + 1 > self.host.depth_cap:
      raise LaunchDenied(f'summon depth cap ({self.host.depth_cap}) reached')
    target = args['target']
    if target not in owner.allow_list:
      from bro.registry import known_names

      if target not in known_names():
        raise LaunchDenied(f'unknown bro {target!r}')
      raise LaunchDenied(f"{target!r} is not in {owner.bro}'s summon allow-list")
    placement = _placement(
      set(request.owner.permits),
      party=args.get('party'),
      isolation=args.get('isolation'),
      manual=request.manual,
    )
    if placement.party == 'join':
      placement = Placement('join', workspace_isolation(request.owner.workspace))
    grant = args.get('grant', [])
    revoke = args.get('revoke', [])
    requested_credentials = sorted(
      {value for value in (*grant, *revoke) if not value.startswith(('@', ':'))}
    )
    if requested_credentials:
      names = ', '.join(requested_credentials)
      raise LaunchDenied(
        f'a summon request cannot grant or revoke credential kind(s): {names}; '
        f'configure them for the child in projects.<identity>.bros.{target}'
      )
    try:
      _, grant_bros, grant_permits = split_scope_overrides(grant)
      _, revoke_bros, _ = split_scope_overrides(revoke)
      harness = get_harness(args.get('harness') or self.host.summon_harness)
      launch_llm_spec(
        harness,
        self.host.workspace.metadata.repo,
        target,
        args.get('llm'),
      )
      layers = configured_scope_layers(self.host.workspace.metadata.repo, target)
      child_allow_list = summon_allow_list(
        target,
        layers=layers,
        grant=grant_bros,
        revoke=revoke_bros,
      )
      child_permits = effective_permits(
        layers,
        grant=grant,
        revoke=revoke,
        strict=True,
      )
    except (RuntimeError, ValueError) as error:
      raise LaunchDenied(str(error)) from error
    beyond = sorted(set(grant_bros) - set(owner.allow_list))
    if beyond:
      raise LaunchDenied(
        'cannot grant summon target(s) the summoner may not summon itself: ' + ', '.join(beyond)
      )
    unheld_permits = sorted(set(grant_permits) - set(request.owner.permits))
    if unheld_permits:
      raise LaunchDenied(
        'cannot grant permit(s) the summoner does not hold: '
        + ', '.join(f':{permit}' for permit in unheld_permits)
      )
    attribution = self.host.peers.attribution_for_mission(self.host.journal, request.owner.mission)
    summoned_by = self._summoned_by(attribution, args)
    worker_permits = frozenset(child_permits)
    facts = BroFacts(
      bro=target,
      allow_list=frozenset(child_allow_list),
      placement=placement,
    )
    if request.manual:
      return Expect(
        {
          'target': target,
          'prompt': args['prompt'],
          'may_summon': sorted(child_allow_list),
          'permits': sorted(child_permits),
          'grant': list(grant),
          'revoke': list(revoke),
          'summoner': summoned_by,
          'repo': self.host.workspace.metadata.repo,
          'into': args.get('into'),
        },
        facts,
        worker_permits,
      )
    return Spawn(
      SummonLaunchSpec(
        target=target,
        prompt=args['prompt'],
        parent=request.owner.workspace,
        parent_tree=request.owner.tree,
        repo=self.host.workspace.repository,
        summoner=summoned_by,
        may_summon=tuple(sorted(child_allow_list)),
        permits=tuple(sorted(child_permits)),
        harness=args.get('harness') or self.host.summon_harness,
        summon_depth=self.host.depth_cap,
        summon_harness=self.host.summon_harness,
        into=args.get('into'),
        hold=args.get('hold'),
        grant=tuple(grant),
        revoke=tuple(revoke),
        share=request.share,
        llm=args.get('llm'),
        party=placement.party,
        isolation=None if placement.party == 'join' else placement.isolation,
        env=dict(self.host.session_env),
      ),
      self.host.summon_spawner,
      facts,
      worker_permits,
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
  may_summon: tuple[str, ...]
  permits: tuple[str, ...]
  grant: tuple[str, ...]
  revoke: tuple[str, ...]
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
  expected = {
    'target',
    'prompt',
    'may_summon',
    'permits',
    'grant',
    'revoke',
    'summoner',
    'repo',
    'into',
  }
  if set(data) != expected:
    raise ValueError('pending bro launch carries an invalid extension shape')
  for key in ('target', 'prompt'):
    if not isinstance(data[key], str) or data[key] == '':
      raise ValueError(f'pending bro launch carries no usable {key}')
  for key in ('may_summon', 'permits', 'grant', 'revoke'):
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
      raise ValueError(f'pending bro launch carries invalid {key}')
  credential_seeds = {
    *split_scope_overrides(data['grant'])[0],
    *split_scope_overrides(data['revoke'])[0],
  }
  if credential_seeds:
    raise ValueError('pending bro launch carries credential seeds')
  for permit in data['permits']:
    from bro.base.scope import permit_name

    permit_name(permit)
  if data['summoner'] is not None and not isinstance(data['summoner'], dict):
    raise ValueError('pending bro launch carries invalid summoner attribution')
  for key in ('repo', 'into'):
    if data[key] is not None and not isinstance(data[key], str):
      raise ValueError(f'pending bro launch carries invalid {key}')
  return PendingBro(
    launch,
    data['target'],
    data['prompt'],
    tuple(data['may_summon']),
    tuple(data['permits']),
    tuple(data['grant']),
    tuple(data['revoke']),
    data['summoner'],
    data['repo'],
    data['into'],
  )
