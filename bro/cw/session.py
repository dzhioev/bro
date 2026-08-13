import dataclasses
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import Optional

from bro.base import credentials, log
from bro.cw.claude_auth import _apply_claude_auth, _load_anthropic_key
from bro.cw.claude_config import (
  _latest_jsonl,
  _provision_host_claude_dir,
  container_claude_state,
  drop_workspace,
  session_trail_pointer,
  workspace_projects_dir,
)
from bro.cw.flags import DEFAULT_HOLD
from bro.launch.root import run_in_container
from bro.launch.scope import (
  LaunchScopeError,
  Surface,
  preflight_scoped_launch,
  scoped_secrets,
)
from bro.workspace.containers import broker_enabled
from bro.workspace.docker import Launch, find_container_id
from bro.workspace.git import resolve_ref
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import KindMismatch, SessionBusy, Workspace
from bro.workspace.paths import project_root, venv_env
from bro.workspace.project import project_config
from bro.workspace.store import ScopedSecrets, log_scoped_secrets, materialize_scoped_store
from bro.workspace.worktrees import ensure_host_worktree, provision_host_worktree


@dataclass(frozen=True)
class SessionSpec:
  """the parameters of a `cw ss` session, as parsed from its argv.

  one object replaces the positional soup threaded through the launch layers;
  credential scoping, the summon allow-list, and the CW_COMMAND / resume-hint env
  all read off it. the grant/revoke lists are normalized to [] (the parser leaves
  them None when unset).
  """

  name: str
  host: bool
  drop: bool
  hold: str
  fast: bool
  grant: list[str]
  revoke: list[str]
  effort: Optional[str]
  resume: bool
  into: Optional[str]
  bro: Optional[str]
  raw: bool
  prompt: Optional[str]
  claude_args: list[str]

  def __post_init__(self) -> None:
    for field in ('grant', 'revoke'):
      if getattr(self, field) is None:
        object.__setattr__(self, field, [])

  @property
  def session_bro(self) -> str:
    """the bro this session runs as — its identity for credential scoping, the
    summon allow-list, and its persona deliveries. `--bro` names it, defaulting
    to the operated project's required default bro."""
    if self.bro is not None:
      return self.bro
    return project_config().default_bro

  @property
  def surface(self) -> Surface:
    """the credential-scoping surface this session launches (`bro.launch.scope.scoped_secrets`)."""
    return Surface.RAW_SESSION if self.raw else Surface.CW_SESSION

  @property
  def kind(self) -> WorkspaceKind:
    """the workspace kind this session runs in."""
    return WorkspaceKind.WORKTREE if self.host else WorkspaceKind.CONTAINER

  def to_command_argv(self) -> list[str]:
    """reconstruct this session as the `cw` argv tokens that launched it — the
    CW_COMMAND the banner renders."""
    if self.resume:
      # a resume takes its whole recipe from the record, so the name is the whole command
      return ['cw', 'resume', self.name]
    flags = {
      '--host': self.host,
      '--fast': self.fast,
      '--drop': self.drop,
      '--raw': self.raw,
    }
    parts = ['cw', 'ss', *(f for f, v in flags.items() if v)]
    if self.hold != DEFAULT_HOLD:
      parts.extend(['--hold', self.hold])
    if self.effort is not None:
      parts.extend(['--effort', self.effort])
    if self.bro is not None:
      parts.extend(['--bro', self.bro])
    for g in self.grant:
      parts.extend(['--grant', g])
    for r in self.revoke:
      parts.extend(['--revoke', r])
    if self.into is not None:
      parts.extend(['--into', self.into])
    parts.extend([self.name, *self.claude_args])
    return parts

  def to_in_place_argv(self) -> list[str]:
    """this session as the inner `cw ss --in-place` invocation (argv after the
    program token), for the outer layer to spawn in a prepared bro.workspace.a second serialization, distinct from to_command_argv: it carries the prompt
    and the forwarded claude args (which to_command_argv deliberately omits) and
    drops the flags the outer already consumed (--host --drop --grant --revoke
    --into). the prompt uses the
    joined `=` form so a prompt starting with `-` can't be mistaken for a flag."""
    flags = {'--fast': self.fast, '--resume': self.resume, '--raw': self.raw}
    parts = ['ss', '--in-place', *(f for f, v in flags.items() if v)]
    if self.hold != DEFAULT_HOLD:
      parts.extend(['--hold', self.hold])
    if self.effort is not None:
      parts.extend(['--effort', self.effort])
    if self.bro is not None:
      parts.extend(['--bro', self.bro])
    if self.prompt is not None:
      parts.append(f'--prompt={self.prompt}')
    parts.extend([self.name, *self.claude_args])
    return parts

  def resume_variant(self) -> 'SessionSpec':
    """this session as a resume: --resume on, create-only inputs cleared. the
    recorded spec `cw resume` relaunches, so a second resume reproduces it
    unchanged."""
    return replace(self, drop=False, resume=True, into=None, prompt=None, claude_args=[])

  def with_scope_overrides(self, *, grant: list[str], revoke: list[str]) -> 'SessionSpec':
    """this spec with further grant/revoke values layered onto its own — how
    `cw resume --grant/--revoke` adjusts the recipe it relaunches.

    A value the spec carries on the opposite side cancels it there rather than
    joining this one, so granting back a revoked credential leaves the computed
    scope's own selection standing instead of pinning that exact name into the
    required tier. Restating a value the spec already carries raises, like every
    other no-op scope override (`credentials.apply_grant_revoke`).
    """
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
class _ScopedLaunch:
  """what the launch preflight resolved for a session: its credential scope, the
  bros it may summon, and the hydrated store a host session materializes."""

  scoped: ScopedSecrets
  may_summon: set[str]
  store: dict[str, bytes]


def record_resume_spec(workspace: Workspace, spec: SessionSpec) -> None:
  """persist the spec `cw resume <name>` relaunches this workspace with.

  Written at every launch, before the session runs, so a session that dies
  without unwinding stays resumable. What lands is the resume variant — the same
  spec whichever launch wrote it, so repeated resumes are fixpoints.
  """
  workspace.resume_file.write_text(json.dumps(spec.resume_variant().dump(), indent=2))


def load_resume_spec(workspace: Workspace) -> Optional[SessionSpec]:
  """the recorded resume spec for a workspace, or None when it has none — a
  workspace whose last session predates the record, or whose record was written
  by an incompatible cw."""
  try:
    data = json.loads(workspace.resume_file.read_text())
  except FileNotFoundError:
    return None
  try:
    return SessionSpec.load(data)
  except (TypeError, ValueError) as e:
    log.warning('ignoring unreadable resume spec for %s: %s', workspace.name, e)
    return None


def _replace_resume_hint(workspace: Workspace) -> None:
  """overwrite claude's misleading `claude --resume <id>` hint with a cw-side one.

  claude prints a two-line resume hint on exit, but the `claude --resume <id>`
  command it suggests doesn't reproduce the session: for a container workspace
  the session jsonl lives at ~/.claude/cw-sessions/<name>/projects/-workspace/
  on the host, not where a bare host-side `claude` would look, and for a host
  worktree it only resolves from inside the worktree and bypasses cw's session
  machinery. We replace it with `cw resume`, which relaunches the session under
  its own recorded flags.

  Only meaningful when stdout is a TTY (otherwise the ANSI escape is junk in
  a log) and a session jsonl exists (otherwise claude didn't print a hint).
  """
  if not sys.stdout.isatty():
    return
  if _latest_jsonl(workspace_projects_dir(workspace)) is None:
    return
  # \033[2A: move cursor up 2 lines (over claude's hint).
  # \033[J:  clear from cursor to end of screen.
  sys.stdout.write('\033[2A\033[J')
  print('Resume this session with:')
  print(f'  cw resume {workspace.name}')


def _finish_session(spec: SessionSpec, workspace: Workspace, code: int) -> int:
  """the post-exit step shared by both modes: `--drop` is honored only on a clean
  exit — a failed session's workspace stays on disk for inspection and recovery —
  and a kept clean exit gets the resume-hint overwrite."""
  if spec.drop:
    if code == 0:
      try:
        drop_workspace(workspace)
        log.info('removed workspace %s', workspace.name)
      except (RuntimeError, OSError) as e:
        log.warning('could not fully remove workspace %s: %s', workspace.name, e)
    else:
      log.info('session exited with code %d; keeping workspace %s', code, workspace.name)
  elif code == 0:
    _replace_resume_hint(workspace)
  return code


def start_session(spec: SessionSpec) -> int:
  container = not spec.host
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    if spec.raw:
      # a --raw session is fenced to the container (the scoped anthropic secret
      # is its auth model), so it cannot degrade to host mode
      log.error('--raw sessions cannot nest inside a container')
      return 1
    log.info('already inside a container; falling back to host mode')
    container = False
    spec = replace(spec, host=True)

  os.environ['CW_COMMAND'] = ' '.join(spec.to_command_argv())
  os.environ['CW_NAME'] = spec.name
  os.environ.setdefault('BRO_SHELL_COMMAND', os.environ['CW_COMMAND'])

  # resolve --into to a commit sha now (a branch/tag/sha → a sha, fetched from
  # origin when not host-local). the container reaches it via /host-repo's shared
  # objects; the host worktree bases its new branch on it. when --into is absent,
  # a new workspace bases on the host checkout's current HEAD — the entrypoint's
  # fallback in container mode, `git worktree add … HEAD` on host — so no default
  # path touches the network. only meaningful at creation — a resume reuses the
  # existing workspace, and its recorded spec carries no --into.
  project = project_root()
  base_ref: Optional[str] = None
  if spec.into is not None:
    base_ref = resolve_ref(project, spec.into)
    if base_ref is None:
      log.error('cannot resolve --into ref: %s', spec.into)
      return 1

  # every precondition that can reject the launch is checked before the workspace
  # is recorded, so a refused launch leaves nothing on disk.
  if not _preflight_session_auth(spec):
    return 1
  bro_name = spec.session_bro
  try:
    scoped, may_summon, store = preflight_scoped_launch(
      scoped_secrets(bro_name, spec.surface),
      bro_name,
      grant=spec.grant,
      revoke=spec.revoke,
    )
  except LaunchScopeError as e:
    log.error('%s', e)
    return 1

  try:
    workspace = Workspace.ensure(spec.name, project, spec.kind)
  except KindMismatch as e:
    log.error('%s', e)
    return 1
  launch = _ScopedLaunch(scoped=scoped, may_summon=may_summon, store=store)
  try:
    with workspace.hold_session_lock():
      record_resume_spec(workspace, spec)
      if container:
        return _container_session(spec, workspace, base_ref, launch)
      return _host_session(spec, workspace, base_ref, launch)
  except SessionBusy as e:
    log.error('%s', e)
    return 1


def _preflight_session_auth(spec: SessionSpec) -> bool:
  """the session flavor's auth precondition, checked before anything is created."""
  if spec.raw:
    if _load_anthropic_key() is not None:
      return True
    log.error(
      '--raw requires the `anthropic` secret to provide an api_key '
      '({"api_key": "..."}); claude --bare does not use OAuth or keychain'
    )
    return False
  if credentials.try_get('claude_code') is not None:
    return True
  log.error(
    'claude_code secret not resolvable — a cw-session authenticates with the '
    'setup-token; mint one with `claude setup-token` and store it in '
    '~/.bro/claude_code_oauth_token'
  )
  return False


def resume_session(name: str, *, grant: list[str], revoke: list[str]) -> int:
  """relaunch a workspace's last session under its recorded spec (`cw resume`),
  with `grant`/`revoke` layered onto the ones the record carries."""
  project = project_root()
  try:
    workspace = Workspace.open(name, project)
  except ValueError as e:
    log.error('%s', e)
    return 1
  spec = load_resume_spec(workspace)
  if spec is None:
    log.error('no session recorded for %s; start one with `cw ss`', name)
    return 1
  try:
    spec = spec.with_scope_overrides(grant=grant, revoke=revoke)
  except ValueError as e:
    log.error('%s', e)
    return 1
  return start_session(spec)


def _container_session(
  spec: SessionSpec, workspace: Workspace, base_ref: Optional[str], launch_scope: _ScopedLaunch
) -> int:
  """launch the session in a container: only machinery — session guard,
  claude-state seeding, broker channel, mounts/env — then the same in-place
  runner host mode spawns crosses the docker boundary as the container command
  (`cw ss --in-place …`, resolved from the clone's venv after the entrypoint
  prepares the tree, so the session runs its workspace's code)."""
  # a container still bound to this workspace's mount outlived the launcher that
  # held the session lock (a killed `cw`), and its claude is still running.
  if find_container_id(workspace.tree) is not None:
    log.error(
      'session already active in the container for workspace %r; refusing to start a second',
      spec.name,
    )
    return 1

  # cheap resume existence guard, mirroring host mode: fail before the container
  # is created. the runner resolves the actual session id from the same dir
  # (derived from its in-container cwd).
  if spec.resume:
    projects_dir = workspace_projects_dir(workspace)
    if _latest_jsonl(projects_dir) is None:
      log.error('no claude session found for %s in %s', spec.name, projects_dir)
      return 1

  bro_name = spec.session_bro
  scoped = launch_scope.scoped

  # CW_BRO themes the whole container (`cw exec` shells render the bro banner),
  # not just the runner's process tree — the runner re-exports it next to claude.
  env: dict[str, str] = {'CW_BRO': bro_name}
  if base_ref is not None:
    # the entrypoint reads CW_BASE_REF to base the fresh clone's worktree branch
    # (the sha's objects are already shared from /host-repo via clone alternates);
    # without it the clone bases on HEAD — the host checkout as cloned.
    env['CW_BASE_REF'] = base_ref
  claude_mounts, claude_env = container_claude_state(spec.name)
  env.update(claude_env)
  launch = Launch(
    name=spec.name,
    command=['cw', *spec.to_in_place_argv()],
    env=env,
    secrets=scoped.required,
    docker_sock=scoped.docker_sock,
    tty=True,
    forward_env=True,
    optional_secrets=scoped.optional,
    extra_mounts=claude_mounts,
  )
  code = run_in_container(
    launch,
    workspace=workspace,
    may_summon=launch_scope.may_summon,
    trail_pointer=session_trail_pointer(spec.name),
  )
  return _finish_session(spec, workspace, code)


def _run_host_root_via_broker(
  workspace: Workspace,
  command: list[str],
  env: dict[str, str],
  may_summon: set[str],
  credential_scope: set[str],
) -> int:
  """run the host session as the broker's root peer: provision its channel socket
  under `var/cw/broker`, point `BROKER_CHANNEL` at it in the runner's env, and
  supervise the runner process on the broker loop until it exits."""
  # imported here, not at module level: _broker_enabled() must be able to short-circuit
  # a launch before anything touches the broker package (see its docstring).
  from bro.launch.spawn import run_root_via_broker
  from bro.launch.summon_control import STATUS_ENV, summon_status_file
  from bro.summon import MAY_SUMMON_ENV, encode_may_summon
  from bro.workspace.spawn import ProcessLaunchSpec

  # a host session reads the summon-status file the host-side SummonControl
  # writes, straight at its host path; a container session reads it through
  # /host-repo (see bro/launch/summon_control.py)
  env[STATUS_ENV] = str(summon_status_file(workspace.project, workspace.name))
  env[MAY_SUMMON_ENV] = encode_may_summon(may_summon)
  launch = ProcessLaunchSpec(command=command, cwd=str(workspace.tree), env=env)
  return run_root_via_broker(
    launch,
    workspace=workspace,
    may_summon=may_summon,
    credential_scope=credential_scope,
    trail_pointer=session_trail_pointer(workspace.name),
  )


def _host_session(
  spec: SessionSpec, workspace: Workspace, base_ref: Optional[str], launch_scope: _ScopedLaunch
) -> int:
  """launch the session in a host worktree: ensure + provision it, then spawn the
  worktree's own `cw ss --in-place` (the in-place runner) inside it — so a session
  always runs its workspace's code, and everything next to claude (argv, MCP
  server, spell delivery, session context) is built from the workspace's checkout. Unless
  `BROKER_DISABLED` short-circuits it (see `_broker_enabled`), the runner is
  supervised as the root peer of a broker, so the session gets its channel
  (`BROKER_CHANNEL` in claude's env) exactly like container mode."""
  project = project_root()
  os.chdir(project)
  worktree = workspace.tree
  scoped = launch_scope.scoped

  # cheap resume existence guard, before the worktree auto-create below could
  # materialize a tree for a mistyped name. the runner resolves the actual
  # session id from the same dir (derived from its cwd).
  if spec.resume and _latest_jsonl(workspace_projects_dir(workspace)) is None:
    log.error('no claude session found for %s in %s', spec.name, workspace_projects_dir(workspace))
    return 1

  # the store is a convenience scope on host, not a boundary (reference/cw.md,
  # "Scoped credential hydration"); the allow-list is enforced by host mode's
  # broker root like container mode's
  log_scoped_secrets(spec.name, scoped.required, scoped.optional)

  if not ensure_host_worktree(worktree, workspace.metadata.branch, base_ref):
    return 1
  if not provision_host_worktree(worktree):
    return 1

  # the worktree's own cw — the inner runner executes the workspace's code, not
  # the launching repo's. a venv without it means the worktree is based on a ref
  # that predates the --in-place contract (see reference/cw.md, "The outer↔inner
  # contract").
  cw_bin = worktree / '.venv' / 'bin' / 'cw'
  if not cw_bin.is_file():
    log.error(
      'no cw in %s — the worktree base predates `cw ss --in-place`; '
      'rebase it onto origin/master (provisioning refreshes the venv) or recreate it',
      cw_bin,
    )
    return 1

  command = [str(cw_bin), *spec.to_in_place_argv()]
  # the auth transform and the private claude state dir are applied here as well
  # as in the runner: the runner is the worktree's own code, which may predate
  # them — the inherited env keeps such a session on the setup-token, isolated
  # from the host's rotating-OAuth /login churn.
  runner_env = venv_env(worktree / '.venv')
  claude_dir = _provision_host_claude_dir(spec.name, worktree, project)
  runner_env['CLAUDE_CONFIG_DIR'] = str(claude_dir)
  runner_env[credentials.REGISTRY_ENV] = str(
    materialize_scoped_store(launch_scope.store, claude_dir / '.bro')
  )
  _apply_claude_auth(runner_env)
  workspace.clear_session_end()
  if broker_enabled():
    code = _run_host_root_via_broker(
      workspace,
      command,
      runner_env,
      launch_scope.may_summon,
      scoped.required | scoped.optional,
    )
  else:
    code = subprocess.run(command, cwd=str(worktree), env=runner_env).returncode
  workspace.record_session_end(code)

  return _finish_session(spec, workspace, code)
