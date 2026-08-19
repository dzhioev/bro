import dataclasses
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import Optional

from bro.base import credentials, log
from bro.launch.broxy import START_SESSION_BROXY_ENV
from bro.llm.llm import LLMSpec
from bro.monitor import SESSION_DIR_ENV, trail_pointer, workspace_session_dir
from bro.workspace.containers import broker_enabled
from bro.workspace.docker import Launch, find_container_id
from bro.workspace.git import resolve_ref
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import KindMismatch, SessionBusy, Workspace
from bro.workspace.paths import (
  CONTAINER_SESSION_DIR,
  ensure_runtime_root,
  in_container,
  project_root,
  venv_env,
)
from bro.workspace.store import ScopedSecrets, log_scoped_secrets, materialize_scoped_store
from bro.workspace.worktrees import ensure_host_worktree, provision_host_worktree
from ride.flags import default_hold
from ride.harness import Harness, get_harness
from ride.root import run_host_process_via_broker, run_in_container
from ride.scope import LaunchScopeError, preflight_scoped_launch, scoped_secrets
from ride.trails import local_trails_mounts


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


def _launch_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  *,
  container: bool,
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
    return _container_session(harness, spec, workspace, base_ref, launch_scope)
  return _host_session(harness, spec, workspace, base_ref, launch_scope)


def _container_session(
  harness: Harness,
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
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
  launch = Launch(
    name=spec.name,
    command=harness.inner_command(spec, workspace),
    env=env,
    secrets=scoped.required,
    optional_secrets=scoped.optional,
    docker_sock=scoped.docker_sock,
    tty=not spec.solo,
    forward_env=True,
    extra_mounts=(
      *extras.mounts,
      *trails_mounts,
      f'{session_state}:{CONTAINER_SESSION_DIR}',
    ),
  )
  return run_in_container(launch, workspace, may_summon=launch_scope.may_summon)


def _host_session(
  harness: Harness,
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
) -> int:
  os.chdir(project_root())
  worktree = workspace.tree
  scoped = launch_scope.scoped
  log_scoped_secrets(spec.name, scoped.required, scoped.optional)
  if not ensure_host_worktree(worktree, workspace.metadata.branch, base_ref):
    return 1
  if not provision_host_worktree(worktree):
    return 1

  inner = harness.inner_command(spec, workspace)
  inner_binary = worktree / '.venv' / 'bin' / inner[0]
  if not inner_binary.is_file():
    log.error(
      'no %s in %s — the worktree base predates the %s console script; '
      'rebase it onto origin/master or recreate it',
      inner[0],
      inner_binary,
      inner[0],
    )
    return 1

  command = [str(inner_binary), *inner[1:]]
  runner_env = venv_env(worktree / '.venv')
  runner_env[credentials.REGISTRY_ENV] = str(
    materialize_scoped_store(launch_scope.store, workspace.path / 'credentials')
  )
  runner_env[SESSION_DIR_ENV] = str(workspace_session_dir(workspace.path))
  runner_env[START_SESSION_BROXY_ENV] = '1'
  if spec.no_trails:
    runner_env['TRAILS_DISABLED'] = '1'
  harness.prepare_host_env(spec, workspace, worktree, runner_env)
  workspace.clear_session_end()
  if broker_enabled():
    code = run_host_process_via_broker(
      workspace,
      command,
      runner_env,
      launch_scope.may_summon,
      scoped.required | scoped.optional,
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


def start_session(spec: SessionSpec) -> int:
  harness = get_harness(spec.harness)
  container = not spec.host
  if in_container():
    log.error(
      'ride cannot start inside a container yet; use `summon` for an isolated sibling '
      'or `bro run|chat` for this container and credential scope'
    )
    return 1
  os.environ['RIDE_COMMAND'] = ' '.join(spec.to_command_argv())
  os.environ['RIDE_WORKSPACE'] = spec.name
  os.environ.setdefault('BRO_SHELL_COMMAND', os.environ['RIDE_COMMAND'])

  project = project_root()
  ensure_runtime_root(project)
  base_ref: Optional[str] = None
  if spec.into is not None:
    base_ref = resolve_ref(project, spec.into)
    if base_ref is None:
      log.error('cannot resolve --into ref: %s', spec.into)
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
      scoped_secrets(spec.bro, recipe, llm_spec=spec.llm_spec),
      spec.bro,
      grant=spec.grant,
      revoke=spec.revoke,
    )
  except LaunchScopeError as error:
    log.error('%s', error)
    return 1

  try:
    workspace = Workspace.ensure(spec.name, project, spec.kind)
  except KindMismatch as error:
    log.error('%s', error)
    return 1
  launch = ScopedLaunch(scoped=scoped, may_summon=may_summon, store=store)
  try:
    with workspace.hold_session_lock():
      record_resume_spec(workspace, spec)
      code = _launch_session(spec, workspace, base_ref, launch, container=container)
      return _finish_session(spec, workspace, code)
  except SessionBusy as error:
    log.error('%s', error)
    return 1


def resume_session(name: str, *, grant: list[str], revoke: list[str]) -> int:
  project = project_root()
  try:
    workspace = Workspace.open(name, project)
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
