import contextlib
import os
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from base import log
from cw.containers import _broker_enabled, run_in_container
from cw.docker import find_container_id
from cw.git import resolve_ref
from cw.paths import _latest_jsonl, _project_root, _venv_env
from cw.secrets import _apply_claude_auth, _container_secrets, _finalize_secrets, _session_bro_name
from cw.summon import summon_allow_list
from cw.workspace import ContainerWorkspace, HostWorktree, Workspace
from cw.worktrees import _ensure_host_worktree, _finish_host_worktree, _provision_host_worktree


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
  auto: bool
  fast: bool
  grant_cred: list[str]
  revoke_cred: list[str]
  grant_summon: list[str]
  revoke_summon: list[str]
  effort: Optional[str]
  resume: bool
  into: Optional[str]
  mcp: Optional[str]
  bro: Optional[str]
  prompt: Optional[str]
  claude_args: list[str]

  def __post_init__(self) -> None:
    for field in ('grant_cred', 'revoke_cred', 'grant_summon', 'revoke_summon'):
      if getattr(self, field) is None:
        object.__setattr__(self, field, [])

  def to_command_argv(self) -> list[str]:
    """reconstruct this session as `cw ss` argv tokens.

    used for CW_COMMAND (the session as launched) and, via resume_variant, the
    exit resume hint — so both carry the same forwarded flags (--auto,
    --grant-cred, --effort, ...).
    """
    flags = {
      '--host': self.host,
      '--auto': self.auto,
      '--fast': self.fast,
      '--drop': self.drop,
      '--resume': self.resume,
    }
    parts = ['cw', 'ss', *(f for f, v in flags.items() if v)]
    if self.effort is not None:
      parts.extend(['--effort', self.effort])
    if self.mcp is not None:
      # joined form: --mcp is nargs='?', so a bare flag directly followed by the
      # name positional would swallow the name as its value
      parts.append(f'--mcp={self.mcp}')
    if self.bro is not None:
      parts.extend(['--bro', self.bro])
    for g in self.grant_cred:
      parts.extend(['--grant-cred', g])
    for r in self.revoke_cred:
      parts.extend(['--revoke-cred', r])
    for g in self.grant_summon:
      parts.extend(['--grant-summon', g])
    for r in self.revoke_summon:
      parts.extend(['--revoke-summon', r])
    if self.into is not None:
      parts.extend(['--into', self.into])
    parts.extend([self.name, *self.claude_args])
    return parts

  def to_in_place_argv(self) -> list[str]:
    """this session as the inner `cw ss --in-place` invocation (argv after the
    program token), for the outer layer to spawn in a prepared workspace.

    a second serialization, distinct from to_command_argv: it carries the prompt
    and the forwarded claude args (which to_command_argv deliberately omits) and
    drops the flags the outer already consumed (--host --drop --grant-cred
    --revoke-cred --grant-summon --revoke-summon --into). the prompt and mcp
    values use the joined `=` form so a prompt
    starting with `-` can't be mistaken for a flag and the nargs='?' --mcp can't
    swallow the name positional."""
    flags = {'--auto': self.auto, '--fast': self.fast, '--resume': self.resume}
    parts = ['ss', '--in-place', *(f for f, v in flags.items() if v)]
    if self.effort is not None:
      parts.extend(['--effort', self.effort])
    if self.mcp is not None:
      parts.append(f'--mcp={self.mcp}')
    if self.bro is not None:
      parts.extend(['--bro', self.bro])
    if self.prompt is not None:
      parts.append(f'--prompt={self.prompt}')
    parts.extend([self.name, *self.claude_args])
    return parts

  def resume_variant(self) -> 'SessionSpec':
    """this session as a resume, for the exit hint: --resume on, create-only
    inputs cleared. --drop / --into / the initial prompt / forwarded claude args
    are rejected alongside --resume (see cli.main), so the hint drops them."""
    return replace(self, drop=False, resume=True, into=None, prompt=None, claude_args=[])


def _replace_resume_hint(workspace: Workspace) -> None:
  """overwrite claude's misleading `claude --resume <id>` hint with a cw-side one.

  claude prints a two-line resume hint on exit, but the `claude --resume <id>`
  command it suggests doesn't reproduce the session: for a container workspace
  the session jsonl lives at ~/.claude/cw-sessions/<name>/projects/-workspace/
  on the host, not where a bare host-side `claude` would look, and for a host
  worktree it only resolves from inside the worktree and bypasses cw's session
  machinery. We replace it with the cw-side resume command that actually works,
  carrying this session's own flags (CW_RESUME_COMMAND, set by start_session)
  so it reproduces the session.

  Only meaningful when stdout is a TTY (otherwise the ANSI escape is junk in
  a log) and a session jsonl exists (otherwise claude didn't print a hint).
  """
  if not sys.stdout.isatty():
    return
  if _latest_jsonl(workspace.claude_projects_dir()) is None:
    return
  resume_command = os.environ['CW_RESUME_COMMAND']
  # \033[2A: move cursor up 2 lines (over claude's hint).
  # \033[J:  clear from cursor to end of screen.
  sys.stdout.write('\033[2A\033[J')
  print('Resume this session with:')
  print(f'  {resume_command}')


def start_session(spec: SessionSpec) -> int:
  os.environ['CW_COMMAND'] = ' '.join(spec.to_command_argv())
  os.environ['CW_NAME'] = spec.name
  os.environ.setdefault('PPP_SHELL_COMMAND', os.environ['CW_COMMAND'])
  os.environ['CW_RESUME_COMMAND'] = ' '.join(spec.resume_variant().to_command_argv())

  container = not spec.host
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    if spec.bro is not None:
      # a --bro session is fenced to the container (the scoped anthropic secret
      # is its auth model), so it cannot degrade to host mode
      log.error('--bro sessions cannot nest inside a container')
      return 1
    log.info('already inside a container; falling back to host mode')
    container = False

  # resolve --into to a commit sha now (a branch/tag/sha → a sha, fetched from
  # origin when not host-local). the container reaches it via /host-repo's shared
  # objects; the host worktree bases its new branch on it. when --into is absent,
  # a new workspace bases on the host checkout's current HEAD — the entrypoint's
  # fallback in container mode, `git worktree add … HEAD` on host — so no default
  # path touches the network. only meaningful at creation — resume reuses the
  # existing workspace, so the two are mutually exclusive (checked in main).
  base_ref: Optional[str] = None
  if spec.into is not None:
    base_ref = resolve_ref(_project_root(), spec.into)
    if base_ref is None:
      log.error('cannot resolve --into ref: %s', spec.into)
      return 1

  if container:
    return _container_session(spec, base_ref)
  return _host_session(spec, base_ref)


def _container_session(spec: SessionSpec, base_ref: Optional[str]) -> int:
  """launch the session in a container: only machinery — session guard, scoped
  secrets, claude-state seeding, broker channel, mounts/env — then the same
  in-place runner host mode spawns crosses the docker boundary as the container
  command (`cw ss --in-place …`, resolved from the clone's venv after the
  entrypoint prepares the tree, so the session runs its workspace's code)."""
  project = _project_root()
  workspace = ContainerWorkspace(spec.name, project)

  # one session per worktree: refuse if a container is already bound to this
  # workspace's mount. a second concurrent session would share /workspace — and
  # its gitignored token-accounting state — and corrupt it.
  if find_container_id(workspace.path) is not None:
    log.error(
      'session already active in the container for workspace %r; refusing to start a second',
      spec.name,
    )
    return 1

  # cheap --resume existence guard, mirroring host mode: fail before the
  # container is created. the runner resolves the actual session id from the
  # same dir (derived from its in-container cwd).
  if spec.resume:
    projects_dir = workspace.claude_projects_dir()
    if _latest_jsonl(projects_dir) is None:
      log.error('no claude session found for %s in %s', spec.name, projects_dir)
      return 1

  if spec.bro is not None:
    # CW_BRO themes the container beyond the runner's own process tree (`cw exec`
    # shells render the bro banner); the runner re-sets it next to claude.
    os.environ['CW_BRO'] = spec.bro

  bro_name = _session_bro_name(spec.bro)
  scoped = _container_secrets(bro_name, mcp=spec.mcp, bro_mode=spec.bro is not None)
  try:
    secrets = _finalize_secrets(scoped.required, grant=spec.grant_cred, revoke=spec.revoke_cred)
    may_summon = summon_allow_list(bro_name, grant=spec.grant_summon, revoke=spec.revoke_summon)
  except ValueError as e:
    log.error('%s', e)
    return 1

  env: dict[str, str] = {}
  if base_ref is not None:
    # the entrypoint reads CW_BASE_REF to base the fresh clone's worktree branch
    # (the sha's objects are already shared from /host-repo via clone alternates);
    # without it the clone bases on HEAD — the host checkout as cloned.
    env['CW_BASE_REF'] = base_ref
  code = run_in_container(
    spec.name,
    ['cw', *spec.to_in_place_argv()],
    drop=spec.drop,
    secrets=secrets,
    optional_secrets=scoped.optional,
    docker_sock=scoped.docker_sock,
    extra_env=env if len(env) > 0 else None,
    may_summon=may_summon,
  )
  if not spec.drop and code == 0:
    _replace_resume_hint(workspace)
  return code


def _run_host_root_via_broker(
  name: str,
  command: list[str],
  worktree: Path,
  project: Path,
  env: dict[str, str],
  may_summon: set[str],
) -> int:
  """run the host session as the broker's root peer: provision its channel socket
  under `var/cw/broker`, point `BROKER_CHANNEL` at it in the runner's env, and
  supervise the runner process on the broker loop until it exits."""
  # imported here, not at module level: _broker_enabled() must be able to short-circuit
  # a launch before anything touches the broker package (see its docstring).
  from cw.spawn import ProcessLaunchSpec, run_root_via_broker
  from cw.summon import STATUS_ENV, summon_status_file

  # a host session reads the summon-status file the host-side SummonControl
  # writes, straight at its host path; the session key is the bare workspace
  # name — container mode prefixes `c:` (see cw/summon.py)
  env[STATUS_ENV] = str(summon_status_file(project, name))
  launch = ProcessLaunchSpec(command=command, cwd=str(worktree), env=env)
  return run_root_via_broker(launch, project, session=name, may_summon=may_summon)


@contextlib.contextmanager
def _held_pidfile(pidfile: Path) -> Generator[None]:
  """hold this process's pid in `pidfile` for the block — the liveness marker
  `Workspace.is_active` reads."""
  pidfile.parent.mkdir(parents=True, exist_ok=True)
  pidfile.write_text(str(os.getpid()))
  try:
    yield
  finally:
    pidfile.unlink(missing_ok=True)


def _host_session(spec: SessionSpec, base_ref: Optional[str]) -> int:
  """launch the session in a host worktree: ensure + provision it, then spawn the
  worktree's own `cw ss --in-place` (the in-place runner) inside it — so a session
  always runs its workspace's code, and everything next to claude (argv, MCP
  server, skills, session context) is built from the workspace's checkout. Unless
  `BROKER_DISABLED` short-circuits it (see `_broker_enabled`), the runner is
  supervised as the root peer of a broker, so the session gets its channel
  (`BROKER_CHANNEL` in claude's env) exactly like container mode."""
  project = _project_root()
  os.chdir(project)
  workspace = HostWorktree(spec.name, project)
  worktree = workspace.path
  branch = f'worktree-{spec.name}'

  # one session per worktree: refuse if a live cw session already owns it (a
  # second concurrent claude would mutate the same files and share the
  # token-accounting state). releases on exit, so re-entry / --resume after a
  # session ends is unaffected.
  if workspace.is_active(set()):
    log.error(
      'session already active on host worktree %r (pid in %s); refusing to start a second',
      spec.name,
      workspace.pidfile,
    )
    return 1

  # cheap --resume existence guard, before the worktree auto-create below could
  # manufacture an empty workspace for a mistyped name. the runner resolves the
  # actual session id from the same dir (derived from its cwd).
  if spec.resume and _latest_jsonl(workspace.claude_projects_dir()) is None:
    log.error('no claude session found for %s in %s', spec.name, workspace.claude_projects_dir())
    return 1

  # the session's summon allow-list, computed before the worktree exists so a bad
  # --grant-summon/--revoke-summon fails without creating anything. host mode is
  # covered like container mode — its broker root enforces the same policy.
  try:
    may_summon = summon_allow_list(
      _session_bro_name(spec.bro), grant=spec.grant_summon, revoke=spec.revoke_summon
    )
  except ValueError as e:
    log.error('%s', e)
    return 1

  if not _ensure_host_worktree(worktree, branch, base_ref):
    return 1
  if not _provision_host_worktree(worktree):
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
  # the auth transform is applied here as well as in the runner: the runner is
  # the worktree's own code, which may predate it — the inherited env keeps such
  # a session on the setup-token instead of the rotating-OAuth /login churn.
  runner_env = _venv_env(worktree / '.venv')
  _apply_claude_auth(runner_env)
  with _held_pidfile(workspace.pidfile):
    if _broker_enabled():
      code = _run_host_root_via_broker(
        spec.name, command, worktree, project, runner_env, may_summon
      )
    else:
      code = subprocess.run(command, cwd=str(worktree), env=runner_env).returncode

  if spec.drop:
    workspace.remove()
  else:
    # the hint first, while claude's own exit hint is still the last two lines
    # on screen; the keep-or-drop offer prints below it
    if code == 0:
      _replace_resume_hint(workspace)
    _finish_host_worktree(workspace, interactive=not spec.auto and sys.stdin.isatty())
  return code
