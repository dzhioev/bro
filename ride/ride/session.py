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
from typing import Optional

from bro.base import configs, credentials, log
from bro.base.scope import scope_override_key, scope_revoke_key
from bro.broker.environment import BROKER_MISSION, BROKER_UPSTREAM
from bro.llm.llm import LLMSpec
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
from ride.bro_worker import PendingBro
from ride.do_ride import (
  CONTAINER_INSTALL_DIRECTORY,
  INSTALL_DIRECTORY_ENV,
  RESOLVED_LLM_ENV,
  command as do_ride_command,
  encode_resolved_llm,
)
from ride.flags import default_hold
from ride.harness import HARNESS_NAMES, Harness, get_harness
from ride.identity import human_git_identity_env
from ride.repository import Repository, hold_repository, is_git_url, open_repository
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
  preflight_scoped_launch,
  scoped_secrets,
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


@dataclass(frozen=True)
class SessionSpec:
  """the harness-neutral recipe recorded for one managed session."""

  name: str
  harness: str
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
  arguments: list[str]
  repo: Optional[str] = None
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH
  summon_harness: str = configs.DEFAULT_SUMMON_HARNESS
  tree: Optional[str] = None
  runtime_bundle: Optional[str] = None
  env: dict[str, str] = field(default_factory=dict)

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
    if self.summon_harness not in HARNESS_NAMES:
      raise ValueError(f'summon harness must be one of {", ".join(HARNESS_NAMES)}')

  @property
  def llm_spec(self) -> LLMSpec:
    return LLMSpec.from_dict(self.resolved_llm)

  @property
  def tty(self) -> bool:
    """whether the session runs on the launcher's terminal."""
    return not self.solo

  def to_command_argv(self) -> list[str]:
    if self.resume:
      return ['ride', 'resume', self.name]
    flags = {'--unboxed': self.isolation is Isolation.UNBOXED, '--no-trails': self.no_trails}
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
    parts.extend(['--harness', self.harness])
    if self.workspace_pinned:
      parts.extend(['--workspace', self.name])
    for value in self.cred:
      parts.extend(['--cred', value])
    for value in self.grant:
      parts.extend(['--grant', value])
    for value in self.revoke:
      parts.extend(['--revoke', value])
    if self.into is not None:
      parts.extend(['--into', self.into])
    for name, value in self.env.items():
      parts.extend(['--env', f'{name}={value}'])
    parts.append(self.bro)
    if self.prompt is not None:
      parts.append(self.prompt)
    if len(self.arguments) > 0:
      parts.extend(['--', *self.arguments])
    return parts

  @property
  def ride_command(self) -> str:
    """the launch line the session runs under, as `RIDE_COMMAND` carries it."""
    return ' '.join(self.to_command_argv())

  def resume_variant(self) -> 'SessionSpec':
    return replace(
      self,
      drop=False,
      hold=default_hold(solo=False, isolation=self.isolation) if self.solo else self.hold,
      solo=False,
      resume=True,
      into=None,
      prompt=None,
      arguments=[],
    )

  def with_scope_overrides(
    self, *, cred: list[str], grant: list[str], revoke: list[str]
  ) -> 'SessionSpec':
    def credential_values(values: list[str]) -> list[str]:
      return [value for value in values if not value.startswith(('@', ':'))]

    def authority_values(values: list[str]) -> list[str]:
      return [value for value in values if value.startswith(('@', ':'))]

    new_grant_credentials = credential_values(grant)
    new_revoke_credentials = credential_values(revoke)
    changed_credential_kinds = {
      *(credentials.parse_name(value)[0] for value in new_grant_credentials),
      *(credentials.parse_name(value)[0] for value in new_revoke_credentials),
    }
    kept_grant_credentials = [
      value
      for value in credential_values(self.grant)
      if credentials.parse_name(value)[0] not in changed_credential_kinds
    ]
    kept_revoke_credentials = [
      value
      for value in credential_values(self.revoke)
      if credentials.parse_name(value)[0] not in changed_credential_kinds
    ]

    new_grant_authority = authority_values(grant)
    new_revoke_authority = authority_values(revoke)
    recorded_grant_authority = authority_values(self.grant)
    recorded_revoke_authority = authority_values(self.revoke)
    for values, recorded, flag in (
      (new_grant_authority, recorded_grant_authority, 'grant'),
      (new_revoke_authority, recorded_revoke_authority, 'revoke'),
    ):
      restated = sorted(set(values) & set(recorded))
      if restated:
        raise ValueError(f'already in the recorded --{flag}: {", ".join(restated)}')
    grant_keys = {scope_override_key(value) for value in new_grant_authority}
    revoke_keys = {scope_revoke_key(value) for value in new_revoke_authority}
    recorded_grant_keys = {scope_override_key(value) for value in recorded_grant_authority}
    recorded_revoke_keys = {scope_revoke_key(value) for value in recorded_revoke_authority}
    kept_grant_authority = [
      value
      for value in recorded_grant_authority
      if scope_override_key(value) not in grant_keys | revoke_keys
    ]
    kept_revoke_authority = [
      value for value in recorded_revoke_authority if scope_revoke_key(value) not in grant_keys
    ]

    new_pick_kinds = {credentials.parse_name(value)[0] for value in cred}
    revoked_kinds = {credentials.parse_name(value)[0] for value in new_revoke_credentials}
    kept_cred = [
      value
      for value in self.cred
      if credentials.parse_name(value)[0] not in new_pick_kinds | revoked_kinds
    ]
    return replace(
      self,
      cred=[*kept_cred, *cred],
      grant=[
        *kept_grant_credentials,
        *new_grant_credentials,
        *kept_grant_authority,
        *(
          value
          for value in new_grant_authority
          if scope_override_key(value) not in recorded_revoke_keys
        ),
      ],
      revoke=[
        *kept_revoke_credentials,
        *new_revoke_credentials,
        *kept_revoke_authority,
        *(
          value
          for value in new_revoke_authority
          if scope_revoke_key(value) not in recorded_grant_keys
        ),
      ],
    )

  def dump(self) -> dict:
    return dataclasses.asdict(self)

  @classmethod
  def load(cls, data: dict) -> 'SessionSpec':
    fields = {field.name for field in dataclasses.fields(cls)}
    if data.keys() != fields:
      raise ValueError(f'unexpected fields: {sorted(data.keys() ^ fields)}')
    values = {**data, 'isolation': Isolation(data['isolation'])}
    return cls(**values)


@dataclass(frozen=True)
class ScopedLaunch:
  scoped: ScopedSecrets
  may_summon: set[str]
  permits: set[str]
  store: dict[str, bytes]
  hydrated_kinds: frozenset[str] = frozenset()


def record_resume_spec(workspace: Workspace, spec: SessionSpec) -> None:
  workspace.resume_file.write_text(json.dumps(spec.resume_variant().dump(), indent=2))


def load_resume_spec(workspace: Workspace) -> Optional[SessionSpec]:
  try:
    data = json.loads(workspace.resume_file.read_text())
  except FileNotFoundError:
    return None
  try:
    spec = SessionSpec.load(data)
    get_harness(spec.harness)
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


def harness_for_workspace(workspace: Workspace) -> Harness:
  spec = load_resume_spec(workspace)
  return get_harness('claude' if spec is None else spec.harness)


def _print_resume_hint(spec: SessionSpec, workspace: Workspace) -> None:
  if not sys.stdout.isatty() or not get_harness(spec.harness).session_exists(workspace):
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
    **summoned_child_env(summoned.may_summon, summoned.permits, summoned.summoner),
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


def container_launch(
  harness: Harness,
  spec: SessionSpec,
  workspace: Workspace,
  scoped: ScopedSecrets,
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
  scoped: ScopedSecrets,
  *,
  human_env: Mapping[str, str],
  runtime_bundle: RuntimeBundle,
  env: Mapping[str, str],
) -> MemberExec:
  """one joined session exec'd into its boxed party's running container: the
  member-scoped session env around the harness's member extras, with the
  surface's own `env` on top."""
  harness = get_harness(spec.harness)
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
    launch_env['RIDE_REPO'] = str(workspace.repo)
    launch_env.update(_attached_tree_env(workspace, _tree_head(workspace.tree)))
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
  harness = get_harness(spec.harness)
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
    runner_env['RIDE_REPO'] = str(workspace.repo)
    runner_env.update(_attached_tree_env(workspace, _tree_head(tree)))
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
  harness = get_harness(spec.harness)
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
      launch_scope.scoped,
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
) -> int:
  harness = get_harness(spec.harness)
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
      try:
        return run_manual_started_party(
          launch,
          workspace,
          credential_scope=launch_scope.scoped,
          claim=lambda: pending_launch.claim(summoned.token, workspace=spec.name),
        )
      except pending_launch.UnknownToken as error:
        log.error('%s', error)
        return 1
    return run_started_party(
      launch,
      workspace,
      may_summon=launch_scope.may_summon,
      permits=launch_scope.permits,
      summon_depth=spec.summon_depth,
      summon_harness=spec.summon_harness,
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
) -> int:
  try:
    runtime_context = (
      resolve_runtime_bundle()
      if spec.runtime_bundle is None
      else resolve_runtime_bundle(spec.runtime_bundle)
    )
    with runtime_context as runtime_bundle:
      runtime_bundle.materialize_host()
      reexec_from_runtime(runtime_bundle.reference, spec.to_command_argv())
      return _start_session(spec, runtime_bundle, repository, summoned)
  except RuntimeBundleError as error:
    log.error('%s', error)
    return 1


def _start_session(
  spec: SessionSpec,
  runtime_bundle: RuntimeBundle,
  repository: Optional[Repository] = None,
  summoned: Optional[PendingBro] = None,
) -> int:
  harness = get_harness(spec.harness)
  os.environ['RIDE_COMMAND'] = spec.ride_command
  os.environ['RIDE_WORKSPACE'] = spec.name
  os.environ[ISOLATION_ENV] = spec.isolation.value
  if spec.repo is None:
    os.environ.pop('RIDE_REPO', None)
  else:
    os.environ['RIDE_REPO'] = spec.repo

  ensure_runtime_root()
  if spec.repo is None:
    repository = None
  elif repository is None and not is_git_url(spec.repo):
    try:
      repository = open_repository(spec.repo)
    except (RuntimeError, ValueError) as error:
      log.error('%s', error)
      return 1
  if repository is not None and repository.identity != spec.repo:
    raise ValueError(
      f'resolved attachment {repository.identity!r} does not match session spec {spec.repo!r}'
    )
  if summoned is not None and not broker_enabled():
    log.error("a manual summon child needs the summoner's broker channel")
    return 1

  auth_error = harness.preflight_auth(spec)
  if auth_error is not None:
    log.error('%s', auth_error)
    return 1
  recipe = harness.scope_recipe()
  try:
    with launch_scope_errors():
      scoped = scoped_secrets(
        spec.bro,
        recipe,
        attachment=spec.repo,
        attachment_repository=repository,
        llm_spec=spec.llm_spec,
        cred=spec.cred,
        grant=spec.grant,
        revoke=spec.revoke,
        recording=not spec.no_trails,
      )
    may_summon, permits, store = preflight_scoped_launch(
      scoped,
      spec.bro,
      attachment=spec.repo,
      attachment_repository=repository,
      grant=spec.grant,
      revoke=spec.revoke,
    )
  except LaunchScopeError as error:
    log.error('%s', error)
    return 1

  if spec.isolation is Isolation.BOXED:
    try:
      runtime_bundle.require_frozen_manifest()
    except RuntimeBundleError as error:
      log.error('%s', error)
      return 1
  repository_context = (
    contextlib.nullcontext(None) if spec.repo is None else hold_repository(spec.repo)
  )
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
    may_summon=may_summon,
    permits=permits,
    store=store,
    hydrated_kinds=store.kinds,
  )
  try:
    with workspace.hold_session_lock():
      record_resume_spec(workspace, spec)
      code = _launch_session(
        spec,
        workspace,
        base_ref,
        launch,
        human_env=human_env,
        runtime_bundle=runtime_bundle,
        container_runtime=container_runtime,
        summoned=summoned,
      )
      return _finish_session(spec, workspace, code)
  except SessionBusy as error:
    log.error('%s', error)
    return 1


def resume_session(name: str, *, cred: list[str], grant: list[str], revoke: list[str]) -> int:
  try:
    workspace = Workspace.open(name)
  except ValueError as error:
    log.error('%s', error)
    return 1
  spec = load_resume_spec(workspace)
  if spec is None:
    log.error('no session recorded for %s; start a new one instead', name)
    return 1
  try:
    spec = spec.with_scope_overrides(cred=cred, grant=grant, revoke=revoke)
  except ValueError as error:
    log.error('%s', error)
    return 1
  return start_session(spec)
