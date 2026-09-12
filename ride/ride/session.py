import contextlib
import dataclasses
import json
import os
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from bro.base import configs, credentials, log
from bro.launch.broker_environment import CHANNEL_ENV, UPSTREAM_ENV
from bro.llm.llm import LLMSpec
from bro.monitor import SESSION_DIR_ENV, trail_pointer, workspace_session_dir
from bro.summon import summoned_child_env
from bro.workspace.git import resolve_head, resolve_ref
from bro.workspace.paths import (
  CONTAINER_SESSION_DIR,
  ISOLATION_ENV,
  ensure_runtime_root,
)
from ride import pending_summon
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
from ride.runtime_bundle import RuntimeBundle, RuntimeBundleError, resolve_runtime_bundle
from ride.scope import (
  LaunchScopeError,
  launch_scope_errors,
  preflight_scoped_launch,
  scoped_secrets,
)
from ride.trails import local_trails_mounts
from ride.workspace.clones import ensure_clone
from ride.workspace.containers import broker_enabled
from ride.workspace.docker import (
  CONTAINER_BROKER_HOST,
  ContainerRuntime,
  ContainerRuntimeResolver,
  Launch,
  find_container_id,
)
from ride.workspace.metadata import BRANCH_ENV, Isolation
from ride.workspace.model import AttachmentMismatch, IsolationMismatch, SessionBusy, Workspace
from ride.workspace.store import (
  ScopedSecrets,
  credential_revoke_kind,
  materialize_scoped_store,
)
from ride.workspace.worktrees import provision_workspace


def _scope_override_key(value: str) -> str:
  if value.startswith('@'):
    return value
  kind, _ = credentials.parse_name(value)
  return kind


def _scope_revoke_key(value: str) -> str:
  if value.startswith('@'):
    return value
  return credential_revoke_kind(value)


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
  harness_options: dict
  repo: Optional[str] = None
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH
  summon_harness: str = configs.DEFAULT_SUMMON_HARNESS
  tree: Optional[str] = None
  runtime_bundle: Optional[str] = None

  def __post_init__(self) -> None:
    if not isinstance(self.isolation, Isolation):
      raise TypeError('session isolation must be boxed or unboxed')
    if self.tree is not None or self.runtime_bundle is not None:
      raise ValueError('external trees and runtime bundles are not supported yet')
    if type(self.summon_depth) is not int or self.summon_depth <= 0:
      raise ValueError('summon depth must be a positive integer')
    if self.summon_harness not in HARNESS_NAMES:
      raise ValueError(f'summon harness must be one of {", ".join(HARNESS_NAMES)}')

  @property
  def llm_spec(self) -> LLMSpec:
    return LLMSpec.from_dict(self.resolved_llm)

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
    parts.extend(['--hold', self.hold])
    if self.llm is not None:
      parts.extend(['--llm', self.llm])
    parts.extend(['--harness', self.harness])
    if self.workspace_pinned:
      parts.extend(['--workspace', self.name])
    for value in self.grant:
      parts.extend(['--grant', value])
    for value in self.revoke:
      parts.extend(['--revoke', value])
    if self.into is not None:
      parts.extend(['--into', self.into])
    parts.extend(get_harness(self.harness).command_options(self))
    parts.append(self.bro)
    if self.prompt is not None:
      parts.append(self.prompt)
    if len(self.arguments) > 0:
      parts.extend(['--', *self.arguments])
    return parts

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

  def with_scope_overrides(self, *, grant: list[str], revoke: list[str]) -> 'SessionSpec':
    grant_keys = {_scope_override_key(name) for name in grant}
    revoke_keys = {_scope_revoke_key(name) for name in revoke}
    recorded_grant_keys = {_scope_override_key(name) for name in self.grant}
    recorded_revoke_keys = {_scope_revoke_key(name) for name in self.revoke}
    for values, own, flag in ((grant, self.grant, 'grant'), (revoke, self.revoke, 'revoke')):
      restated = sorted(set(values) & set(own))
      if len(restated) > 0:
        raise ValueError(f'already in the recorded --{flag}: {", ".join(restated)}')
    kept_grant = [
      name for name in self.grant if _scope_override_key(name) not in revoke_keys | grant_keys
    ]
    kept_revoke = [name for name in self.revoke if _scope_override_key(name) not in grant_keys]
    return replace(
      self,
      grant=[
        *kept_grant,
        *(name for name in grant if _scope_override_key(name) not in recorded_revoke_keys),
      ],
      revoke=[
        *kept_revoke,
        *(name for name in revoke if _scope_override_key(name) not in recorded_grant_keys),
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


def harness_for_workspace(workspace: Workspace) -> Harness:
  spec = load_resume_spec(workspace)
  return get_harness('claude' if spec is None else spec.harness)


def _print_resume_hint(spec: SessionSpec, workspace: Workspace) -> None:
  if not sys.stdout.isatty() or not get_harness(spec.harness).session_exists(workspace):
    return
  print('Resume this session with:')
  print(f'  ride resume {workspace.name}')


def _summoned_env(
  summoned: pending_summon.PendingSummon, spec: SessionSpec, address: str
) -> dict[str, str]:
  """the env that makes a launch the manual summon child the token names: the
  summoner's channel, the quest the child answers (its token), and the
  summoned-child facts."""
  return {
    UPSTREAM_ENV: address,
    'BROKER_QUEST': summoned.token,
    'RIDE_WORKSPACE': spec.name,
    **summoned_child_env(summoned.may_summon, summoned.summoner),
  }


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
  forward_env: bool,
  env: Mapping[str, str],
  mounts: Collection[str],
) -> Launch:
  """one managed session's container launch, whichever surface spawns it: the
  neutral session env and mounts around the harness's extras, with the
  surface's own `env` and `mounts` on top."""
  session_state = workspace_session_dir(workspace.path)
  # created before the container launch so the bind mount finds it and does not
  # materialize it root-owned
  session_state.mkdir(parents=True, exist_ok=True)
  extras = harness.container_extras(spec, workspace, scoped)
  launch_env: dict[str, str] = {
    'RIDE_BRO': spec.bro,
    ISOLATION_ENV: Isolation.BOXED.value,
    RESOLVED_LLM_ENV: encode_resolved_llm(spec.resolved_llm),
    INSTALL_DIRECTORY_ENV: CONTAINER_INSTALL_DIRECTORY,
    SESSION_DIR_ENV: str(CONTAINER_SESSION_DIR),
    **human_env,
    **extras.env,
  }
  if workspace.metadata.branch is not None:
    launch_env[BRANCH_ENV] = workspace.metadata.branch
  if spec.no_trails:
    # a run that records nothing binds no trails root
    launch_env['TRAILS_DISABLED'] = '1'
  launch_env.update(env)
  trails_mounts = () if spec.no_trails else local_trails_mounts(scoped)
  return Launch(
    name=spec.name,
    command=do_ride_command(spec, harness_flags=harness.session_flags(spec)),
    env=launch_env,
    secrets=scoped.required,
    optional_secrets=scoped.optional,
    credential_selection=scoped.selection,
    tty=not spec.solo,
    forward_env=forward_env,
    image=container_runtime.image,
    runtime_bundle_hash=container_runtime.bundle_hash,
    extra_mounts=(
      *extras.mounts,
      *trails_mounts,
      f'{session_state}:{CONTAINER_SESSION_DIR}',
      *mounts,
    ),
    repo=repo,
    base_ref=base_ref,
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
  forward_env: bool,
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
      forward_env=forward_env,
      env=env,
      mounts=mounts,
    )

  if len(mounts) > 0:
    raise ValueError('an unboxed started party cannot carry container mounts')
  runtime_bundle.materialize_host()
  tree = workspace.tree
  if repository is None:
    tree.mkdir(parents=True, exist_ok=True)
  else:
    branch = workspace.metadata.branch
    if branch is None:
      raise ValueError('attached unboxed workspace has no recorded branch')
    ensure_clone(repository, tree, branch, base_ref)
    if not provision_workspace(tree):
      raise RuntimeError(f'failed to provision workspace {tree}')

  session_command = do_ride_command(spec, harness_flags=harness.session_flags(spec))
  command = [str(runtime_bundle.host_venv / 'bin' / session_command[0]), *session_command[1:]]
  runner_env = runtime_bundle.host_session_env()
  if not forward_env:
    runner_env.pop(CHANNEL_ENV, None)
    runner_env.pop(UPSTREAM_ENV, None)
  runner_env.update(env)
  runner_env['RIDE_BRO'] = spec.bro
  runner_env[ISOLATION_ENV] = Isolation.UNBOXED.value
  runner_env['RIDE_HOST_WORKSPACE'] = str(tree)
  runner_env.update(human_env)
  if workspace.repo is not None:
    runner_env['RIDE_REPO'] = str(workspace.repo)
    if workspace.metadata.branch is None:
      raise ValueError('attached unboxed workspace has no recorded branch')
    runner_env[BRANCH_ENV] = workspace.metadata.branch
  else:
    runner_env.pop('RIDE_REPO', None)
    runner_env.pop(BRANCH_ENV, None)
  store_directory = materialize_scoped_store(launch_scope.store, credential_directory)
  runner_env['BRO_STORE'] = str(store_directory)
  runner_env['BRO_INSTALL_KINDS'] = ' '.join(sorted(launch_scope.hydrated_kinds))
  runner_env[INSTALL_DIRECTORY_ENV] = str(install_directory)
  runner_env[RESOLVED_LLM_ENV] = encode_resolved_llm(spec.resolved_llm)
  runner_env[SESSION_DIR_ENV] = str(workspace_session_dir(workspace.path))
  if spec.no_trails:
    runner_env['TRAILS_DISABLED'] = '1'
  harness.prepare_unboxed_env(spec, workspace, tree, runner_env)
  return ProcessLaunch(
    command=command,
    cwd=str(tree),
    env=runner_env,
    interactive=not spec.solo,
  )


def _launch_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  *,
  human_env: dict[str, str],
  runtime_bundle: RuntimeBundle,
  container_runtime: ContainerRuntimeResolver,
  summoned: Optional[pending_summon.PendingSummon] = None,
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
      forward_env=summoned is None,
      env=summoned_env,
      credential_directory=workspace.path / 'credentials',
      install_directory=workspace.path / 'environment',
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
        claim=lambda: pending_summon.claim(summoned.token, workspace=spec.name),
      )
    except pending_summon.UnknownToken as error:
      log.error('%s', error)
      return 1
  return run_started_party(
    launch,
    workspace,
    may_summon=launch_scope.may_summon,
    summon_depth=spec.summon_depth,
    summon_harness=spec.summon_harness,
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
  summoned: Optional[pending_summon.PendingSummon] = None,
) -> int:
  try:
    with resolve_runtime_bundle() as runtime_bundle:
      return _start_session(spec, runtime_bundle, repository, summoned)
  except RuntimeBundleError as error:
    log.error('%s', error)
    return 1


def _start_session(
  spec: SessionSpec,
  runtime_bundle: RuntimeBundle,
  repository: Optional[Repository] = None,
  summoned: Optional[pending_summon.PendingSummon] = None,
) -> int:
  harness = get_harness(spec.harness)
  os.environ['RIDE_COMMAND'] = ' '.join(spec.to_command_argv())
  os.environ['RIDE_WORKSPACE'] = spec.name
  os.environ[ISOLATION_ENV] = spec.isolation.value
  if spec.repo is None:
    os.environ.pop('RIDE_REPO', None)
  else:
    os.environ['RIDE_REPO'] = spec.repo
  os.environ.setdefault('BRO_SHELL_COMMAND', os.environ['RIDE_COMMAND'])

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
  recipe = harness.scope_recipe(spec.harness_options)
  if spec.no_trails:
    recipe = dataclasses.replace(recipe, optional_baseline=frozenset())
  try:
    with launch_scope_errors():
      scoped = scoped_secrets(
        spec.bro,
        recipe,
        attachment=spec.repo,
        llm_spec=spec.llm_spec,
        grant=spec.grant,
        revoke=spec.revoke,
      )
    may_summon, store = preflight_scoped_launch(
      scoped, spec.bro, grant=spec.grant, revoke=spec.revoke
    )
  except LaunchScopeError as error:
    log.error('%s', error)
    return 1

  if spec.isolation is Isolation.UNBOXED:
    runtime_bundle.materialize_host()
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
          base_ref = resolve_head(repository.git_dir, Path(summoned.parent_workspace))
          if base_ref is None:
            log.error("cannot read the summoner's HEAD at %s", summoned.parent_workspace)
            return 1
      container_runtime = ContainerRuntimeResolver(runtime_bundle, repository)
      human_env = human_git_identity_env(repository)
      workspace = Workspace.ensure(spec.name, repository, spec.isolation)
  except (AttachmentMismatch, IsolationMismatch, RuntimeError, ValueError) as error:
    log.error('%s', error)
    return 1
  launch = ScopedLaunch(
    scoped=scoped,
    may_summon=may_summon,
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


def resume_session(name: str, *, grant: list[str], revoke: list[str]) -> int:
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
    spec = spec.with_scope_overrides(grant=grant, revoke=revoke)
  except ValueError as error:
    log.error('%s', error)
    return 1
  return start_session(spec)
