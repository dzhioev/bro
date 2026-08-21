import contextlib
import dataclasses
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from bro.base import credentials, log
from bro.launch.broxy import START_SESSION_BROXY_ENV
from bro.llm.llm import LLMSpec
from bro.monitor import SESSION_DIR_ENV, trail_pointer, workspace_session_dir
from bro.summon import MAY_SUMMON_ENV, SUMMONED_ENV, SUMMONER_ENV, encode_may_summon
from bro.workspace.git import resolve_head, resolve_ref
from bro.workspace.paths import (
  CONTAINER_SESSION_DIR,
  ensure_runtime_root,
  in_container,
)
from ride import pending_summon
from ride.flags import default_hold
from ride.harness import Harness, get_harness
from ride.inner import inner_command
from ride.repository import Repository, hold_repository, is_git_url, open_repository
from ride.root import run_host_process_via_broker, run_in_container, run_summoned_in_container
from ride.runtime_bundle import RuntimeBundle, RuntimeBundleError, resolve_runtime_bundle
from ride.scope import LaunchScopeError, preflight_scoped_launch, scoped_secrets
from ride.trails import local_trails_mounts
from ride.workspace.containers import broker_enabled, container_broker_enabled
from ride.workspace.docker import (
  CONTAINER_BROKER_ADDRESS,
  CONTAINER_BROKER_SOCK,
  ContainerRuntimeResolver,
  Launch,
  find_container_id,
)
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import AttachmentMismatch, KindMismatch, SessionBusy, Workspace
from ride.workspace.store import ScopedSecrets, log_scoped_secrets, materialize_scoped_store
from ride.workspace.worktrees import ensure_host_worktree, provision_host_worktree


@dataclass(frozen=True)
class SessionSpec:
  """the harness-neutral recipe recorded for one managed session."""

  name: str
  harness: str
  workspace_pinned: bool
  host: bool
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

  @property
  def llm_spec(self) -> LLMSpec:
    return LLMSpec.from_dict(self.resolved_llm)

  @property
  def kind(self) -> WorkspaceKind:
    return WorkspaceKind.WORKTREE if self.host else WorkspaceKind.CONTAINER

  def to_command_argv(self) -> list[str]:
    if self.resume:
      return ['ride', 'resume', self.name]
    flags = {'--host': self.host, '--no-trails': self.no_trails}
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
      hold=default_hold(solo=False, host=self.host) if self.solo else self.hold,
      solo=False,
      resume=True,
      into=None,
      prompt=None,
      arguments=[],
    )

  def with_scope_overrides(self, *, grant: list[str], revoke: list[str]) -> 'SessionSpec':
    for values, own, flag in ((grant, self.grant, 'grant'), (revoke, self.revoke, 'revoke')):
      restated = sorted(set(values) & set(own))
      if len(restated) > 0:
        raise ValueError(f'already in the recorded --{flag}: {", ".join(restated)}')
    kept_grant = [name for name in self.grant if name not in revoke]
    kept_revoke = [name for name in self.revoke if name not in grant]
    return replace(
      self,
      grant=[*kept_grant, *(name for name in grant if name not in self.revoke)],
      revoke=[*kept_revoke, *(name for name in revoke if name not in self.grant)],
    )

  def dump(self) -> dict:
    return dataclasses.asdict(self)

  @classmethod
  def load(cls, data: dict) -> 'SessionSpec':
    fields = {field.name for field in dataclasses.fields(cls)}
    if data.keys() != fields:
      raise ValueError(f'unexpected fields: {sorted(data.keys() ^ fields)}')
    return cls(**data)


@dataclass(frozen=True)
class ScopedLaunch:
  scoped: ScopedSecrets
  may_summon: set[str]
  store: dict[str, bytes]


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
  summoner's channel, the summoned-child facts, and the workspace name the
  session announces in its `started` (the base-ref source for its own summons)."""
  env = {
    'BROKER_CHANNEL': address,
    SUMMONED_ENV: '1',
    MAY_SUMMON_ENV: encode_may_summon(summoned.may_summon),
    'RIDE_WORKSPACE': spec.name,
  }
  if summoned.summoner is not None:
    env[SUMMONER_ENV] = json.dumps(summoned.summoner, ensure_ascii=False, separators=(',', ':'))
  return env


def _launch_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  *,
  container: bool,
  runtime_bundle: RuntimeBundle,
  container_runtime: ContainerRuntimeResolver,
  summoned: Optional[pending_summon.PendingSummon] = None,
) -> int:
  harness = get_harness(spec.harness)
  if container and find_container_id(workspace.tree) is not None:
    log.error(
      'session already active in the container for workspace %r; refusing to start a second',
      spec.name,
    )
    return 1
  # created before the container launch so the bind mount finds it and does not
  # materialize it root-owned
  workspace_session_dir(workspace.path).mkdir(parents=True, exist_ok=True)
  if not spec.resume:
    trail_pointer.clear(trail_pointer.session_pointer(workspace.path))
  elif not harness.session_exists(workspace):
    log.error('%s', harness.missing_session_error(workspace))
    return 1
  if container:
    return _container_session(
      harness, spec, workspace, base_ref, launch_scope, container_runtime, summoned
    )
  return _host_session(
    harness,
    spec,
    workspace,
    base_ref,
    launch_scope,
    runtime_bundle,
    container_runtime,
    summoned,
  )


def _container_session(
  harness: Harness,
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  container_runtime: ContainerRuntimeResolver,
  summoned: Optional[pending_summon.PendingSummon],
) -> int:
  scoped = launch_scope.scoped
  session_state = workspace_session_dir(workspace.path)
  env: dict[str, str] = {
    'RIDE_BRO': spec.bro,
    SESSION_DIR_ENV: str(CONTAINER_SESSION_DIR),
  }
  if base_ref is not None:
    env['RIDE_BASE_REF'] = base_ref
  extras = harness.container_extras(spec, workspace, scoped)
  env.update(extras.env)
  if spec.no_trails:
    # a run that records nothing binds no trails root
    env['TRAILS_DISABLED'] = '1'
  trails_mounts = () if spec.no_trails else local_trails_mounts(scoped)
  summoned_mounts = ()
  if summoned is not None:
    env.update(_summoned_env(summoned, spec, CONTAINER_BROKER_ADDRESS))
    summoned_mounts = (f'{summoned.socket}:{CONTAINER_BROKER_SOCK}',)
  resolved_runtime = container_runtime.resolve()
  launch = Launch(
    name=spec.name,
    command=inner_command(spec, harness_flags=harness.inner_flags(spec)),
    env=env,
    secrets=scoped.required,
    optional_secrets=scoped.optional,
    tty=not spec.solo,
    forward_env=True,
    image=resolved_runtime.image,
    runtime_bundle_hash=resolved_runtime.bundle_hash,
    extra_mounts=(
      *extras.mounts,
      *trails_mounts,
      *summoned_mounts,
      f'{session_state}:{CONTAINER_SESSION_DIR}',
    ),
    repo=workspace.repository,
  )
  if summoned is not None:
    try:
      return run_summoned_in_container(
        launch, workspace, claim=lambda: pending_summon.claim(summoned.token)
      )
    except pending_summon.UnknownToken as error:
      log.error('%s', error)
      return 1
  return run_in_container(launch, workspace, may_summon=launch_scope.may_summon)


def _host_session(
  harness: Harness,
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  runtime_bundle: RuntimeBundle,
  container_runtime: ContainerRuntimeResolver,
  summoned: Optional[pending_summon.PendingSummon] = None,
) -> int:
  worktree = workspace.tree
  scoped = launch_scope.scoped
  log_scoped_secrets(spec.name, scoped.required, scoped.optional)
  repository = workspace.repository
  if repository is None:
    worktree.mkdir(parents=True, exist_ok=True)
  else:
    branch = workspace.metadata.branch
    if branch is None:
      raise ValueError('attached host workspace has no recorded branch')
    if not ensure_host_worktree(repository.git_dir, worktree, branch, base_ref):
      return 1
    if not provision_host_worktree(worktree):
      return 1

  inner = inner_command(spec, harness_flags=harness.inner_flags(spec))
  command = [str(runtime_bundle.host_venv / 'bin' / inner[0]), *inner[1:]]
  runner_env = runtime_bundle.host_session_env()
  runner_env['RIDE_HOST_WORKSPACE'] = str(worktree)
  if workspace.repo is not None:
    runner_env['RIDE_REPO'] = str(workspace.repo)
  else:
    runner_env.pop('RIDE_REPO', None)
  registry = materialize_scoped_store(launch_scope.store, workspace.path / 'credentials')
  runner_env[credentials.REGISTRY_ENV] = str(registry)
  runner_env.update(
    credentials.install_hooks(
      credentials.load_registry(registry), workspace.path / 'environment', runner_env
    )
  )
  runner_env[SESSION_DIR_ENV] = str(workspace_session_dir(workspace.path))
  runner_env[START_SESSION_BROXY_ENV] = '1'
  if spec.no_trails:
    runner_env['TRAILS_DISABLED'] = '1'
  harness.prepare_host_env(spec, workspace, worktree, runner_env)
  workspace.clear_session_end()
  if summoned is not None:
    # no broker of its own: the session broxy connects to the summoner's socket,
    # and the token is claimed only once nothing fallible is left before the run
    runner_env.update(_summoned_env(summoned, spec, f'unix:{summoned.socket}'))
    try:
      pending_summon.claim(summoned.token)
    except pending_summon.UnknownToken as error:
      log.error('%s', error)
      return 1
    code = subprocess.run(command, cwd=str(worktree), env=runner_env).returncode
  elif broker_enabled():
    code = run_host_process_via_broker(
      workspace,
      command,
      runner_env,
      launch_scope.may_summon,
      scoped.required | scoped.optional,
      container_runtime,
      interactive=not spec.solo,
    )
  else:
    runner_env.pop(START_SESSION_BROXY_ENV, None)
    runner_env.pop('BROKER_CHANNEL', None)
    code = subprocess.run(command, cwd=str(worktree), env=runner_env).returncode
  workspace.record_session_end(code)
  return code


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
  if in_container():
    log.error(
      'ride cannot start inside a container yet; use `summon` for an isolated sibling '
      'or `bro run|chat` for this container and credential scope'
    )
    return 1
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
  container = not spec.host
  os.environ['RIDE_COMMAND'] = ' '.join(spec.to_command_argv())
  os.environ['RIDE_WORKSPACE'] = spec.name
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
  if summoned is not None and not (container_broker_enabled() if container else broker_enabled()):
    log.error(
      "a manual summon child needs the summoner's broker channel"
      + ('; use --host on this platform' if container else '')
    )
    return 1

  auth_error = harness.preflight_auth(spec)
  if auth_error is not None:
    log.error('%s', auth_error)
    return 1
  recipe = harness.scope_recipe(spec.harness_options)
  if spec.no_trails:
    recipe = dataclasses.replace(recipe, optional_baseline=frozenset())
  try:
    scoped, may_summon, store = preflight_scoped_launch(
      scoped_secrets(spec.bro, recipe, attachment=spec.repo, llm_spec=spec.llm_spec),
      spec.bro,
      grant=spec.grant,
      revoke=spec.revoke,
    )
  except LaunchScopeError as error:
    log.error('%s', error)
    return 1

  if spec.host:
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
      workspace = Workspace.ensure(spec.name, repository, spec.kind)
  except (AttachmentMismatch, KindMismatch, RuntimeError, ValueError) as error:
    log.error('%s', error)
    return 1
  launch = ScopedLaunch(scoped=scoped, may_summon=may_summon, store=store)
  try:
    with workspace.hold_session_lock():
      record_resume_spec(workspace, spec)
      code = _launch_session(
        spec,
        workspace,
        base_ref,
        launch,
        container=container,
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
