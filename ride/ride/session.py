import contextlib
import dataclasses
import json
import os
import socket
import sys
import tempfile
from collections.abc import Collection, Generator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional, cast

from bro.authority.authority import Authority, Cap
from bro.authority.records import (
  dump_authority,
  dump_request,
  load_authority,
  load_request,
)
from bro.authority.rules import Mission
from bro.base import configs, credentials, log
from bro.broker.environment import BROKER_MISSION, BROKER_UPSTREAM, Talk
from bro.harness import harness_name
from bro.llm import providers
from bro.llm.llm import Recipe
from bro.monitor import (
  SESSION_DIR_ENV,
  party_member_dir,
  trail_pointer,
  workspace_party_dir,
  workspace_session_dir,
)
from bro.summon import RUNTIME_ENV, summoned_child_env
from bro.workspace.git import resolve_head, resolve_ref, rev_parse_commit
from bro.workspace.paths import (
  BASE_SHA_ENV,
  BRANCH_ENV,
  CONTAINER_PARTY_DIR,
  CONTAINER_SESSION_DIR,
  ISOLATION_ENV,
  TRAILS_ROOT_ENV,
  ensure_runtime_root,
  ride_trails_dir,
  workspace_dir,
)
from ride import pending_launch
from ride.authority_launch import credentials_for, hydrate, request as authority_request
from ride.authority_reading import Reading
from ride.authority_scope import checked_recipe
from ride.bro_worker import PendingBro
from ride.do_ride import (
  CONTAINER_INSTALL_DIRECTORY,
  INSTALL_DIRECTORY_ENV,
  RESOLVED_LLM_ENV,
  command as do_ride_command,
  encode_resolved_llm,
)
from ride.harness import SessionHarness, get_harness
from ride.identity import human_git_identity_env
from ride.repository import (
  Repository,
  hold_repository,
  recorded_origin_url,
)
from ride.root import ProcessLaunch, run_manual_started_party, run_started_party
from ride.runtime_bundle import (
  RuntimeBundle,
  RuntimeBundleError,
  reexec_from_runtime,
  resolve_runtime_bundle,
)
from ride.scope import (
  LaunchScopeError,
  launch_scope_errors,
)
from ride.session_env import env_additions
from ride.trails import local_trails_mounts
from ride.workspace.clones import ensure_clone
from ride.workspace.containers import broker_enabled
from ride.workspace.docker import (
  CONTAINER_BROKER_HOST,
  MEMBER_BASELINE_ENV,
  ContainerRuntime,
  ContainerRuntimeResolver,
  Launch,
  MemberExec,
  find_container_id,
  member_install_dir,
)
from ride.workspace.metadata import Isolation
from ride.workspace.model import AttachmentMismatch, IsolationMismatch, SessionBusy, Workspace
from ride.workspace.store import ScopedSecrets, materialize_scoped_store
from ride.workspace.worktrees import provision_workspace

_SESSION_INPUT_FIELDS = frozenset(
  {
    'cred',
    'grant',
    'revoke',
    'limit',
    'llm',
    'resolved_llm',
    'summon_depth',
    'summon_harness',
    'mission',
    'recipe_options',
    'export',
    'harness_set',
  }
)


@dataclass(frozen=True)
class SessionSpec:
  """the harness-neutral recipe recorded for one managed session."""

  name: str
  harness: str | None
  workspace_pinned: bool
  isolation: Isolation
  drop: bool
  no_trails: bool
  hold: str
  cred: list[str]
  grant: list[str]
  revoke: list[str]
  llm: Optional[str]
  resolved_llm: dict
  solo: bool
  resume: bool
  into: Optional[str]
  bro: str
  prompt: Optional[str]
  subject: Optional[str]
  repo: Optional[str] = None
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH
  summon_harness: str | None = None
  tree: Optional[str] = None
  runtime_bundle: Optional[str] = None
  env: dict[str, str] = field(default_factory=dict)
  limit: list[str] = field(default_factory=list)
  recipe_options: dict[str, str | bool] = field(default_factory=dict)
  export: bool = False
  harness_set: bool = True
  authority_request: dict = field(default_factory=dict)
  operator_gift: dict = field(default_factory=dict)
  operator_cap: dict = field(default_factory=dict)
  operator_values: list[str] = field(default_factory=list)
  override_operation: dict = field(default_factory=dict)
  mission: Mission | None = field(default=None, repr=False, compare=False)

  def __post_init__(self) -> None:
    if not isinstance(self.isolation, Isolation):
      raise TypeError('session isolation must be boxed or unboxed')
    env_additions(self.env)
    if self.tree is not None:
      if self.repo is not None:
        raise ValueError('an external tree requires a detached session')
      if self.isolation is not Isolation.UNBOXED:
        raise ValueError('an external tree requires unboxed isolation')
      if not Path(self.tree).is_absolute():
        raise ValueError('external tree path must be absolute')
    if self.runtime_bundle is not None and self.runtime_bundle == '':
      raise ValueError('runtime bundle reference must not be empty')
    if type(self.summon_depth) is not int or self.summon_depth <= 0:
      raise ValueError('summon depth must be a positive integer')
    if self.harness is not None:
      harness_name(self.harness)
    if self.summon_harness is not None:
      harness_name(self.summon_harness)

  @property
  def resolved_harness(self) -> str:
    if self.harness is None:
      raise ValueError('session has no resolved harness')
    return self.harness

  @property
  def llm_spec(self) -> Recipe:
    return Recipe.from_dict(self.resolved_llm)

  @property
  def tty(self) -> bool:
    """whether the session runs on the launcher's terminal."""
    return not self.solo

  def to_command_argv(self) -> list[str]:
    if self.resume:
      return ['ride', 'resume', self.name]
    flags = {
      '--unboxed': self.isolation is Isolation.UNBOXED,
      '--no-trails': self.no_trails,
      '--export': self.export,
    }
    if not self.solo:
      flags['--drop'] = self.drop
    elif not self.workspace_pinned:
      flags['--keep'] = not self.drop
    verb = 'solo' if self.solo else 'along'
    parts = ['ride', verb, *(flag for flag, enabled in flags.items() if enabled)]
    if self.repo is not None:
      parts.extend(['--repo', self.repo])
    if self.tree is not None:
      parts.extend(['--tree', self.tree])
    if self.runtime_bundle is not None and Path(self.runtime_bundle).is_absolute():
      parts.extend(['--runtime-bundle', self.runtime_bundle])
    parts.extend(['--hold', self.hold])
    if self.llm is not None:
      parts.extend(['--llm', self.llm])
    if self.harness_set:
      parts.extend(['--harness', self.resolved_harness])
    for name, value in self.recipe_options.items():
      parts.extend(
        ['--' + name] if name in providers.SELECTION_FLAGS else ['--' + name, str(value)]
      )
    if self.workspace_pinned:
      parts.extend(['--workspace', self.name])
    for value in self.cred:
      parts.extend(['--cred', value])
    for value in self.grant:
      parts.extend(['--grant', value])
    for value in self.revoke:
      parts.extend(['--revoke', value])
    for value in self.limit:
      parts.extend(['--limit', value])
    if self.into is not None:
      parts.extend(['--into', self.into])
    for name, value in self.env.items():
      parts.extend(['--env', f'{name}={value}'])
    parts.append(self.bro)
    if self.prompt is not None:
      parts.append(self.prompt)
    return parts

  @property
  def ride_command(self) -> str:
    """the launch line the session runs under, as `RIDE_COMMAND` carries it."""
    return ' '.join(self.to_command_argv())

  def resume_variant(self) -> 'SessionSpec':
    return replace(
      self,
      drop=False,
      solo=False,
      resume=True,
      into=None,
      prompt=None,
    )

  def with_scope_overrides(
    self, *, cred: list[str], grant: list[str], revoke: list[str]
  ) -> 'SessionSpec':
    return replace(self, cred=cred, grant=grant, revoke=revoke)

  def dump(self) -> dict:
    return {
      item.name: getattr(self, item.name)
      for item in dataclasses.fields(self)
      if item.name not in _SESSION_INPUT_FIELDS
    }

  @classmethod
  def load(cls, data: dict) -> 'SessionSpec':
    fields = {item.name for item in dataclasses.fields(cls)} - _SESSION_INPUT_FIELDS
    if data.keys() != fields or not data.get('authority_request'):
      raise ValueError(
        f'unreadable session record: unexpected fields {sorted(data.keys() ^ fields)}'
      )
    return cls(
      **{**data, 'isolation': Isolation(data['isolation'])},
      cred=[],
      grant=[],
      revoke=[],
      llm=None,
      resolved_llm={},
    )


@dataclass(frozen=True)
class ScopedLaunch:
  scoped: ScopedSecrets
  store: dict[str, bytes]
  hydrated_kinds: frozenset[str] = frozenset()
  mission: Mission | None = None


def record_resume_spec(workspace: Workspace, spec: SessionSpec) -> None:
  workspace.resume_file.write_text(json.dumps(spec.resume_variant().dump(), indent=2))


def load_resume_spec(workspace: Workspace) -> Optional[SessionSpec]:
  try:
    data = json.loads(workspace.resume_file.read_text())
  except FileNotFoundError:
    return None
  try:
    spec = SessionSpec.load(data)
    get_harness(spec.resolved_harness)
    return spec
  except (TypeError, ValueError) as error:
    log.warning('ignoring unreadable resume spec for %s: %s', workspace.name, error)
    return None


def recorded_runtime_reference(name: str) -> Optional[str]:
  """Read the runtime bootstrap field without loading the session record."""
  try:
    data = json.loads((workspace_dir(name) / 'resume.json').read_text())
  except (FileNotFoundError, json.JSONDecodeError):
    return None
  if not isinstance(data, dict) or data.get('runtime_bundle') is None:
    return None
  reference = data['runtime_bundle']
  if not isinstance(reference, str) or reference == '':
    raise ValueError(f'recorded runtime bundle reference is invalid for {name}')
  return reference


def harness_for_workspace(workspace: Workspace) -> SessionHarness | None:
  spec = load_resume_spec(workspace)
  return None if spec is None else get_harness(spec.resolved_harness)


def _print_resume_hint(spec: SessionSpec, workspace: Workspace) -> None:
  if not sys.stdout.isatty() or not get_harness(spec.resolved_harness).session_exists(workspace):
    return
  print('Resume this session with:')
  print(f'  ride resume {workspace.name}')


def _summoned_env(summoned: PendingBro, spec: SessionSpec, address: str) -> dict[str, str]:
  """the env that makes a launch the manual summon child the token names: the
  summoner's channel, the quest the child answers (its token), and the
  summoned-child facts."""
  from bro.broker.brotocol import encode_talk
  from bro.broker.environment import BROKER_TALK

  return {
    BROKER_UPSTREAM: address,
    BROKER_MISSION: summoned.token,
    BROKER_TALK: encode_talk(summoned.talk),
    'RIDE_WORKSPACE': spec.name,
    **summoned_child_env(summoned.summoner),
  }


def _tree_head(tree: Path) -> str:
  head = rev_parse_commit(tree, 'HEAD')
  if head is None:
    raise RuntimeError(f'cannot read HEAD of workspace tree {tree}')
  return head


def _base_commit(workspace: Workspace, base_ref: Optional[str]) -> str:
  """the commit an attached session's tree starts at: the tree's HEAD where its
  clone exists (a resume), else the base the clone is about to be made at."""
  if (workspace.tree / '.git').is_dir():
    return _tree_head(workspace.tree)
  if base_ref is None:
    raise ValueError('attached launch has no base to clone at')
  return base_ref


def _attached_tree_env(workspace: Workspace, base_sha: str) -> dict[str, str]:
  branch = workspace.metadata.branch
  if branch is None:
    raise ValueError('attached workspace has no recorded branch')
  return {BRANCH_ENV: branch, BASE_SHA_ENV: base_sha}


def _attached_session_env(workspace: Workspace, base_sha: str) -> dict[str, str]:
  if workspace.repo is None:
    raise ValueError('attached workspace has no repository')
  environment = {
    'RIDE_REPO': str(workspace.repo),
    **_attached_tree_env(workspace, base_sha),
  }
  repo_url = recorded_origin_url(workspace.tree)
  if repo_url is not None:
    environment['RIDE_REPO_URL'] = repo_url
  return environment


def container_launch(
  harness: SessionHarness,
  spec: SessionSpec,
  workspace: Workspace,
  launch_scope: ScopedLaunch,
  container_runtime: ContainerRuntime,
  *,
  repo: Optional[Repository | Path],
  base_ref: Optional[str],
  human_env: Mapping[str, str],
  env: Mapping[str, str],
  mounts: Collection[str],
) -> Launch:
  """one managed session's container launch, whichever surface spawns it: the
  neutral session env and mounts around the harness's extras, with the
  surface's own `env` and `mounts` on top, and the spec's `--env` additions as
  the container's own lowest layer."""
  scoped = launch_scope.scoped
  session_state = workspace_session_dir(workspace.path)
  party_dir = workspace_party_dir(workspace.path)
  # created before the container launch so the bind mounts find them and do not
  # materialize them root-owned
  session_state.mkdir(parents=True, exist_ok=True)
  party_dir.mkdir(parents=True, exist_ok=True)
  extras = harness.container_extras(spec, workspace, scoped)
  launch_env: dict[str, str] = {
    'RIDE_BRO': spec.bro,
    'RIDE_COMMAND': spec.ride_command,
    ISOLATION_ENV: Isolation.BOXED.value,
    RESOLVED_LLM_ENV: encode_resolved_llm(spec.resolved_llm),
    INSTALL_DIRECTORY_ENV: CONTAINER_INSTALL_DIRECTORY,
    SESSION_DIR_ENV: str(CONTAINER_SESSION_DIR),
    **human_env,
    **extras.env,
  }
  if workspace.repo is not None:
    launch_env.update(_attached_tree_env(workspace, _base_commit(workspace, base_ref)))
  if spec.no_trails:
    # a run that records nothing binds no trails root
    launch_env['TRAILS_DISABLED'] = '1'
  launch_env.update(env)
  trails_mounts = () if spec.no_trails else local_trails_mounts(scoped)
  return Launch(
    name=spec.name,
    command=do_ride_command(spec),
    env=launch_env,
    additions=dict(spec.env),
    secrets=scoped.required,
    optional_secrets=scoped.optional,
    credential_selection=scoped.selection,
    credential_store=launch_scope.store,
    hydrated_kinds=launch_scope.hydrated_kinds,
    tty=spec.tty,
    image=container_runtime.image,
    runtime_bundle_hash=container_runtime.bundle_hash,
    extra_mounts=(
      *extras.mounts,
      *trails_mounts,
      f'{session_state}:{CONTAINER_SESSION_DIR}',
      f'{party_dir}:{CONTAINER_PARTY_DIR}',
      *mounts,
    ),
    repo=repo,
    base_ref=base_ref,
  )


def boxed_member_launch(
  spec: SessionSpec,
  workspace: Workspace,
  member: str,
  launch_scope: ScopedLaunch,
  *,
  human_env: Mapping[str, str],
  runtime_bundle: RuntimeBundle,
  env: Mapping[str, str],
) -> MemberExec:
  """one joined session exec'd into its boxed party's running container: the
  member-scoped session env around the harness's member extras, with the
  surface's own `env` on top."""
  scoped = launch_scope.scoped
  harness = get_harness(spec.resolved_harness)
  container_id = find_container_id(workspace.tree)
  if container_id is None:
    raise RuntimeError(f'party {workspace.name!r} has no running container to join')
  records = party_member_dir(workspace.path, member)
  member_root = CONTAINER_PARTY_DIR / member
  # created host-side before the exec so the record dirs exist under the party
  # mount when the member's wrapper writes its process record and its recorder
  # opens the local trails store
  workspace_session_dir(records).mkdir(parents=True, exist_ok=True)
  (records / 'trails').mkdir(parents=True, exist_ok=True)
  launch_env: dict[str, str] = {
    **spec.env,
    **MEMBER_BASELINE_ENV,
    'RIDE_BRO': spec.bro,
    'RIDE_WORKSPACE': workspace.name,
    'RIDE_HOST_WORKSPACE': str(workspace.tree),
    'RIDE_HOST': socket.gethostname(),
    ISOLATION_ENV: Isolation.BOXED.value,
    RESOLVED_LLM_ENV: encode_resolved_llm(spec.resolved_llm),
    INSTALL_DIRECTORY_ENV: str(member_install_dir(member)),
    SESSION_DIR_ENV: str(workspace_session_dir(member_root)),
    # a local-trails member records under its own party records rather than the
    # first session's /var/ride/trails bind, which its scope may not have
    TRAILS_ROOT_ENV: str(member_root / 'trails'),
    RUNTIME_ENV: str(runtime_bundle.host_root),
    **human_env,
  }
  if workspace.repo is not None:
    launch_env.update(_attached_session_env(workspace, _tree_head(workspace.tree)))
  if spec.no_trails:
    launch_env['TRAILS_DISABLED'] = '1'
  harness.prepare_boxed_member_env(spec, records, member_root, launch_env)
  launch_env.update(env)
  return MemberExec(
    container=container_id,
    member=member,
    command=do_ride_command(spec),
    env=launch_env,
    secrets=scoped.required,
    optional_secrets=scoped.optional,
    credential_selection=scoped.selection,
    credential_store=launch_scope.store,
    hydrated_kinds=launch_scope.hydrated_kinds,
  )


def prepared_unboxed_session_launch(
  spec: SessionSpec,
  workspace: Workspace,
  launch_scope: ScopedLaunch,
  *,
  human_env: Mapping[str, str],
  runtime_bundle: RuntimeBundle,
  env: Mapping[str, str],
  credential_directory: Path,
  install_directory: Path,
  records_directory: Path,
) -> ProcessLaunch:
  """Describe a session process in an already-prepared unboxed workspace tree:
  the neutral session env around the harness's extras, with the surface's own
  `env` on top."""
  harness = get_harness(spec.resolved_harness)
  tree = workspace.tree
  session_command = do_ride_command(spec)
  command = [str(runtime_bundle.host_venv / 'bin' / session_command[0]), *session_command[1:]]
  runner_env = runtime_bundle.host_session_env(tree, tty=spec.tty, additions=spec.env)
  runner_env['RIDE_BRO'] = spec.bro
  runner_env['RIDE_COMMAND'] = spec.ride_command
  runner_env[RUNTIME_ENV] = str(runtime_bundle.host_root)
  runner_env[ISOLATION_ENV] = Isolation.UNBOXED.value
  runner_env['RIDE_HOST'] = socket.gethostname()
  runner_env['RIDE_HOST_WORKSPACE'] = str(tree)
  runner_env.update(human_env)
  if workspace.repo is not None:
    runner_env.update(_attached_session_env(workspace, _tree_head(tree)))
  store_directory = materialize_scoped_store(launch_scope.store, credential_directory)
  runner_env['BRO_STORE'] = str(store_directory)
  runner_env['BRO_INSTALL_KINDS'] = ' '.join(sorted(launch_scope.hydrated_kinds))
  runner_env[INSTALL_DIRECTORY_ENV] = str(install_directory)
  runner_env[RESOLVED_LLM_ENV] = encode_resolved_llm(spec.resolved_llm)
  runner_env[SESSION_DIR_ENV] = str(workspace_session_dir(records_directory))
  if spec.no_trails:
    runner_env['TRAILS_DISABLED'] = '1'
    runner_env.pop(TRAILS_ROOT_ENV, None)
  else:
    runner_env.pop('TRAILS_DISABLED', None)
    runner_env[TRAILS_ROOT_ENV] = str(ride_trails_dir())
  harness.prepare_unboxed_env(spec, records_directory, tree, runner_env)
  runner_env.update(env)
  return ProcessLaunch(
    command=command,
    cwd=str(tree),
    env=runner_env,
    interactive=spec.tty,
  )


def started_party_launch(
  spec: SessionSpec,
  workspace: Workspace,
  repository: Optional[Repository],
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  *,
  human_env: Mapping[str, str],
  runtime_bundle: RuntimeBundle,
  container_runtime: ContainerRuntimeResolver,
  env: Mapping[str, str],
  mounts: Collection[str] = (),
  credential_directory: Path,
  install_directory: Path,
) -> Launch | ProcessLaunch:
  """Prepare one started party and describe its first session for supervision."""
  harness = get_harness(spec.resolved_harness)
  if workspace.isolation is Isolation.BOXED:
    if find_container_id(workspace.tree) is not None:
      raise RuntimeError(
        f'session already active in the container for workspace {spec.name!r}; '
        'refusing to start a second'
      )
    return container_launch(
      harness,
      spec,
      workspace,
      launch_scope,
      container_runtime.resolve(),
      repo=workspace.repo if isinstance(workspace.repo, Path) else repository,
      base_ref=base_ref,
      human_env=human_env,
      env={RUNTIME_ENV: str(runtime_bundle.host_root), **env},
      mounts=mounts,
    )

  if len(mounts) > 0:
    raise ValueError('an unboxed started party cannot carry container mounts')
  tree = workspace.tree
  if repository is None:
    if workspace.metadata.tree is None:
      tree.mkdir(parents=True, exist_ok=True)
    elif not tree.is_dir():
      raise RuntimeError(f'external workspace tree is gone: {tree}')
  else:
    branch = workspace.metadata.branch
    if branch is None:
      raise ValueError('attached unboxed workspace has no recorded branch')
    ensure_clone(repository, tree, branch, base_ref)
    if not provision_workspace(tree):
      raise RuntimeError(f'failed to provision workspace {tree}')

  return prepared_unboxed_session_launch(
    spec,
    workspace,
    launch_scope,
    human_env=human_env,
    runtime_bundle=runtime_bundle,
    env=env,
    credential_directory=credential_directory,
    install_directory=install_directory,
    records_directory=workspace.path,
  )


@contextlib.contextmanager
def _session_secret_directories(workspace: Workspace) -> Generator[tuple[Path, Path]]:
  if workspace.isolation is Isolation.BOXED:
    yield workspace.path / 'credentials', workspace.path / 'environment'
    return
  with tempfile.TemporaryDirectory(prefix=f'ride-{workspace.name}-secrets-') as directory:
    root = Path(directory)
    yield root / 'store', root / 'environment'


def _launch_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  *,
  human_env: dict[str, str],
  runtime_bundle: RuntimeBundle,
  container_runtime: ContainerRuntimeResolver,
  summoned: Optional[PendingBro] = None,
  commit_id: str | None = None,
) -> int:
  harness = get_harness(spec.resolved_harness)
  if workspace.isolation is Isolation.BOXED and find_container_id(workspace.tree) is not None:
    log.error(
      'session already active in the container for workspace %r; refusing to start a second',
      spec.name,
    )
    return 1
  if not spec.resume:
    trail_pointer.clear(trail_pointer.session_pointer(workspace.path))
  elif not harness.session_exists(workspace):
    log.error('%s', harness.missing_session_error(workspace))
    return 1
  summoned_env: Mapping[str, str] = {}
  if summoned is not None:
    host = CONTAINER_BROKER_HOST if workspace.isolation is Isolation.BOXED else None
    summoned_env = _summoned_env(summoned, spec, summoned.address(host))
  with _session_secret_directories(workspace) as (credential_directory, install_directory):
    try:
      launch = started_party_launch(
        spec,
        workspace,
        workspace.repository,
        base_ref,
        launch_scope,
        human_env=human_env,
        runtime_bundle=runtime_bundle,
        container_runtime=container_runtime,
        env=summoned_env,
        credential_directory=credential_directory,
        install_directory=install_directory,
      )
    except (RuntimeError, ValueError) as error:
      log.error('%s', error)
      return 1
    if summoned is not None:
      if commit_id is None:
        raise ValueError('claim check: a manual launch has no commit id')
      from ride.operator import claim_launch

      try:
        return run_manual_started_party(
          launch,
          workspace,
          credential_scope=launch_scope.scoped,
          claim=lambda: claim_launch(summoned.root, summoned.token, commit_id, spec.name),
          applier_root=summoned.root,
          applier_mission=summoned.token,
        )
      except (pending_launch.UnknownToken, ValueError, RuntimeError) as error:
        log.error('%s', error)
        return 1
    return run_started_party(
      launch,
      workspace,
      mission=cast(Mission, launch_scope.mission),
      session_env=spec.env,
      credential_scope=launch_scope.scoped,
      container_runtime=container_runtime,
      runtime_bundle=runtime_bundle,
    )


def _finish_session(spec: SessionSpec, workspace: Workspace, code: int) -> int:
  if spec.drop:
    if code == 0:
      try:
        workspace.remove()
        log.info('removed workspace %s', workspace.name)
      except (RuntimeError, OSError) as error:
        log.warning('could not fully remove workspace %s: %s', workspace.name, error)
    else:
      log.info('session exited with code %d; keeping workspace %s', code, workspace.name)
  elif code == 0:
    _print_resume_hint(spec, workspace)
  return code


def start_session(
  spec: SessionSpec,
  repository: Optional[Repository] = None,
  summoned: Optional[PendingBro] = None,
  reexec_argv: Optional[list[str]] = None,
) -> int:
  try:
    runtime_context = (
      resolve_runtime_bundle()
      if spec.runtime_bundle is None
      else resolve_runtime_bundle(spec.runtime_bundle)
    )
    with runtime_context as runtime_bundle:
      runtime_bundle.materialize_host()
      reexec_from_runtime(
        runtime_bundle.reference,
        spec.to_command_argv() if reexec_argv is None else reexec_argv,
      )
      return _start_session(spec, runtime_bundle, repository, summoned)
  except RuntimeBundleError as error:
    log.error('%s', error)
    return 1


@dataclass(frozen=True)
class PreparedSession:
  spec: SessionSpec
  workspace: Workspace
  base_ref: str | None
  scope: ScopedLaunch
  human_env: dict[str, str]
  container_runtime: ContainerRuntimeResolver
  commit_id: str | None = None
  summoned: PendingBro | None = None


def _start_session(
  spec: SessionSpec,
  runtime_bundle: RuntimeBundle,
  repository: Repository | None = None,
  summoned: PendingBro | None = None,
) -> int:
  ensure_runtime_root()
  context = contextlib.nullcontext(None) if spec.repo is None else hold_repository(spec.repo)
  try:
    with context as resolved:
      prepared = _prepare_session(spec, runtime_bundle, resolved, summoned)
  except (RuntimeError, ValueError) as error:
    log.error('%s', error)
    return 1
  if isinstance(prepared, int):
    return prepared
  spec, workspace = prepared.spec, prepared.workspace
  try:
    with workspace.hold_session_lock():
      record_resume_spec(workspace, spec)
      code = _launch_session(
        spec,
        workspace,
        prepared.base_ref,
        prepared.scope,
        human_env=prepared.human_env,
        runtime_bundle=runtime_bundle,
        container_runtime=prepared.container_runtime,
        summoned=prepared.summoned,
        commit_id=prepared.commit_id,
      )
      return _finish_session(spec, workspace, code)
  except SessionBusy as error:
    log.error('%s', error)
    return 1


def _prepare_session(
  spec: SessionSpec,
  runtime_bundle: RuntimeBundle,
  repository: Repository | None,
  summoned: PendingBro | None,
) -> PreparedSession | int:
  os.environ['RIDE_COMMAND'] = spec.ride_command
  os.environ['RIDE_WORKSPACE'] = spec.name
  os.environ[ISOLATION_ENV] = spec.isolation.value
  os.environ.pop('RIDE_REPO_URL', None)
  if spec.repo is None:
    os.environ.pop('RIDE_REPO', None)
  else:
    os.environ['RIDE_REPO'] = spec.repo
  if repository is not None and repository.identity != spec.repo:
    raise ValueError(
      f'resolved attachment {repository.identity!r} does not match session spec {spec.repo!r}'
    )
  if summoned is not None and not broker_enabled():
    log.error("a manual summon child needs the summoner's broker channel")
    return 1

  commit_id = None
  try:
    if summoned is None:
      if repository is not None and repository.is_url and spec.into is not None:
        commit = resolve_ref(repository.git_dir, spec.into)
        if commit is None:
          raise ValueError(f'existence check: into: cannot resolve {spec.into!r}')
        repository = replace(repository, tree_ref=commit)
      reading = Reading.take(repository)
      if spec.authority_request:
        requested = load_request(spec.authority_request, reading.syntax)
        requested = replace(
          requested, values={**requested.values, 'harness': spec.resolved_harness}
        )
        if spec.cred or spec.grant or spec.revoke or spec.limit:
          from ride.authority_launch import resume_request

          requested, operator_gift, operator_cap = resume_request(requested, spec, reading)
        else:
          operator_gift = (
            load_authority(spec.operator_gift, reading.syntax)
            if spec.operator_gift
            else Authority()
          )
          operator_cap = (
            load_authority(spec.operator_cap, reading.syntax) if spec.operator_cap else Cap()
          )
      else:
        from ride.bro_worker import BroType

        fields = {
          'target': spec.bro,
          'harness': reading.resolve_harness(
            spec.harness if spec.harness_set else None, target=spec.bro
          ),
          'hold': spec.hold,
          'placement': spec.isolation.value,
          'timeout': None,
          'pass': spec.cred,
          'grant': spec.grant,
          'revoke': spec.revoke,
          'limit': spec.limit,
        }
        fields.update(spec.recipe_options)
        if spec.export:
          fields['export'] = True
        if spec.llm is not None:
          fields['llm'] = spec.llm
        requested, _ = authority_request(
          BroType, fields, reading, reading.operator(), forward=False
        )
        operator_gift, operator_cap = Authority(), Cap()
      mission = reading.rules.start(requested, reading.operator(), reading)
      reading.rules.gift(reading.operator(), operator_gift, mission.descriptor)
      mission = replace(
        mission,
        authority=mission.authority.join(operator_gift).meet(operator_cap),
        operator_values=frozenset(requested.values),
        operator_gift=operator_gift,
        operator_cap=operator_cap,
      )
      values = mission.descriptor.values
      spec = replace(
        spec,
        bro=cast(str, values['target']),
        harness=cast(str, values['harness']),
        hold=cast(str, values['hold']),
        isolation=Isolation(cast(str, values['placement'])),
      )
      scoped = credentials_for(mission, recording=not spec.no_trails)
      files, kinds = hydrate(scoped)
      spec = replace(
        spec,
        mission=mission,
        harness_set=True,
        operator_values=sorted(mission.operator_values),
        authority_request=dump_request(requested, mission.descriptor),
        operator_gift=dump_authority(operator_gift, mission.descriptor),
        operator_cap=dump_authority(operator_cap, mission.descriptor),
        resolved_llm=checked_recipe(requested, mission.descriptor, reading).dump(),
        summon_depth=reading.summon_depth,
        summon_harness=reading.summon_harness,
      )
    else:
      from bro.authority.records import load_values
      from ride.operator import commit_launch

      committed = commit_launch(summoned.root, summoned.token, spec)
      commit_id = committed['commit']
      values = load_values(committed['descriptor']['values'])
      summoned = replace(
        summoned, launch=replace(summoned.launch, talk=tuple(sorted(cast(Talk, values['talk']))))
      )
      selection = {
        name.removeprefix('cred.'): cast(str, value)
        for name, value in values.items()
        if name.startswith('cred.')
      }
      scoped = ScopedSecrets(set(committed['required']), set(committed['optional']), selection)
      files, kinds = hydrate(scoped)
      mission = None
      spec = replace(
        spec,
        bro=cast(str, values['target']),
        isolation=Isolation(cast(str, values['placement'])),
        harness=cast(str, values['harness']),
        harness_set=True,
        hold=cast(str, values['hold']),
        resolved_llm=committed['resolved_llm'],
        authority_request=committed['request'],
        operator_values=committed['operator_values'],
        runtime_bundle=None,
      )
    harness = get_harness(spec.resolved_harness)
    with launch_scope_errors():
      auth_error = harness.preflight_auth(spec, scoped)
    if auth_error is not None:
      raise ValueError(auth_error)
  except (LaunchScopeError, ValueError, credentials.SecretNotFound, RuntimeError) as error:
    log.error('%s', error)
    return 1

  if spec.isolation is Isolation.BOXED:
    try:
      runtime_bundle.require_frozen_manifest()
    except RuntimeBundleError as error:
      log.error('%s', error)
      return 1
  repository_context = contextlib.nullcontext(repository)
  try:
    with repository_context as resolved_repository:
      repository = resolved_repository
      base_ref = None if repository is None else repository.default_base
      if spec.into is not None:
        if repository is None:
          log.error('--into requires --repo')
          return 1
        base_ref = resolve_ref(repository.git_dir, spec.into)
        if base_ref is None:
          log.error('cannot resolve --into ref: %s', spec.into)
          return 1
      if summoned is not None:
        if summoned.into is not None:
          if repository is None:
            log.error('a detached manual summon cannot name an into ref')
            return 1
          base_ref = resolve_ref(repository.git_dir, summoned.into)
          if base_ref is None:
            log.error('cannot resolve the summon into ref: %s', summoned.into)
            return 1
        elif repository is not None:
          base_ref = resolve_head(repository.git_dir, Path(summoned.owner_tree))
          if base_ref is None:
            log.error("cannot read the summoner's HEAD at %s", summoned.owner_tree)
            return 1
      if repository is not None and base_ref is None:
        base_ref = rev_parse_commit(repository.git_dir, 'HEAD')
        if base_ref is None:
          log.error('cannot read HEAD of %s', repository.git_dir)
          return 1
      container_runtime = ContainerRuntimeResolver(runtime_bundle, repository)
      human_env = human_git_identity_env(repository)
      workspace = Workspace.ensure(
        spec.name,
        repository,
        spec.isolation,
        tree=None if spec.tree is None else Path(spec.tree),
      )
  except (AttachmentMismatch, IsolationMismatch, RuntimeError, ValueError) as error:
    log.error('%s', error)
    return 1
  launch = ScopedLaunch(
    scoped=scoped,
    store=files,
    hydrated_kinds=kinds,
    mission=mission,
  )
  return PreparedSession(
    spec, workspace, base_ref, launch, human_env, container_runtime, commit_id, summoned
  )


def _resume_argv(
  name: str, *, cred: list[str], grant: list[str], revoke: list[str], limit: list[str]
) -> list[str]:
  parts = ['ride', 'resume']
  for flag, values in (
    ('--cred', cred),
    ('--grant', grant),
    ('--revoke', revoke),
    ('--limit', limit),
  ):
    for value in values:
      parts.extend([flag, value])
  parts.append(name)
  return parts


def resume_session(
  name: str, *, cred: list[str], grant: list[str], revoke: list[str], limit: list[str] | None = None
) -> int:
  limit = [] if limit is None else limit
  reexec_argv = _resume_argv(name, cred=cred, grant=grant, revoke=revoke, limit=limit)
  try:
    workspace = Workspace.open(name)
  except ValueError as error:
    log.error('%s', error)
    return 1
  from ride.credential_overrides import settle_resume

  try:
    with workspace.hold_session_lock():
      settle_resume(workspace.path)
      spec = load_resume_spec(workspace)
  except (RuntimeError, ValueError, OSError) as error:
    log.error('%s', error)
    return 1
  if spec is None:
    log.error('no session recorded for %s; start a new one instead', name)
    return 1
  try:
    spec = replace(spec.with_scope_overrides(cred=cred, grant=grant, revoke=revoke), limit=limit)
  except ValueError as error:
    log.error('%s', error)
    return 1
  return start_session(spec, reexec_argv=reexec_argv)
