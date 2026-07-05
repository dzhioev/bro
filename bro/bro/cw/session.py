import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from base import log
from cw.containers import _broker_enabled, _replace_container_resume_hint, run_in_container
from cw.docker import find_container_id
from cw.paths import _latest_jsonl, _project_root, _venv_env
from cw.secrets import _DEFAULT_CW_BRO, _apply_claude_auth, _container_secrets, _finalize_secrets
from cw.workspace import ContainerWorkspace, HostWorktree
from cw.worktrees import _ensure_host_worktree, _finish_host_worktree, _provision_host_worktree


def _resolve_base_ref(into: str) -> Optional[str]:
  # resolve --into (branch/tag/sha) to a commit sha in the host repo. when the
  # ref isn't host-local, fetch it from origin and resolve FETCH_HEAD: a feature
  # branch pushed to origin from a container has no host-local ref, so basing a
  # later workspace on it (the `/feature` per-stage flow) would otherwise fail. the
  # container reaches the fetched objects via /host-repo's shared store. returns
  # None when neither the local lookup nor the origin fetch resolves.
  root = _project_root()
  local = subprocess.run(
    ['git', 'rev-parse', '--verify', f'{into}^{{commit}}'],
    cwd=root,
    capture_output=True,
    text=True,
  )
  if local.returncode == 0:
    return local.stdout.strip()
  if subprocess.run(['git', 'fetch', 'origin', into], cwd=root).returncode != 0:
    return None
  fetched = subprocess.run(
    ['git', 'rev-parse', '--verify', 'FETCH_HEAD^{commit}'],
    cwd=root,
    capture_output=True,
    text=True,
  )
  return fetched.stdout.strip() if fetched.returncode == 0 else None


@dataclass(frozen=True)
class SessionSpec:
  """the parameters of a `cw ss` session, as parsed from its argv.

  one object replaces the positional soup threaded through the launch layers;
  credential scoping and the CW_COMMAND / resume-hint env both read off it.
  grant / revoke are normalized to [] (the parser leaves them None when unset).
  """

  name: str
  container: bool
  drop: bool
  auto: bool
  fast: bool
  grant: list[str]
  revoke: list[str]
  effort: Optional[str]
  resume: bool
  into: Optional[str]
  mcp: Optional[str]
  bro: Optional[str]
  prompt: Optional[str]
  claude_args: list[str]

  def __post_init__(self) -> None:
    if self.grant is None:
      object.__setattr__(self, 'grant', [])
    if self.revoke is None:
      object.__setattr__(self, 'revoke', [])

  def to_command_argv(self) -> list[str]:
    """reconstruct this session as `cw ss` argv tokens.

    used for CW_COMMAND (the session as launched) and, via resume_variant, the
    exit resume hint — so both carry the same forwarded flags (--auto, --grant,
    --effort, ...).
    """
    flags = {
      '-c': self.container,
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
    program token), for the outer layer to spawn in a prepared workspace.

    a second serialization, distinct from to_command_argv: it carries the prompt
    and the forwarded claude args (which to_command_argv deliberately omits) and
    drops the flags the outer already consumed (-c --drop --grant --revoke
    --into). the prompt and mcp values use the joined `=` form so a prompt
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


def start_session(spec: SessionSpec) -> int:
  os.environ['CW_COMMAND'] = ' '.join(spec.to_command_argv())
  os.environ['CW_NAME'] = spec.name
  os.environ.setdefault('PPP_SHELL_COMMAND', os.environ['CW_COMMAND'])
  os.environ['CW_RESUME_COMMAND'] = ' '.join(spec.resume_variant().to_command_argv())

  container = spec.container
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    if spec.bro is not None:
      # a --bro session is fenced to the container (the scoped anthropic secret
      # is its auth model), so it cannot degrade to host mode
      log.error('--bro sessions cannot nest inside a container')
      return 1
    log.info('already inside a container; falling back to host mode')
    container = False

  # resolve --into to a commit sha now (a branch/tag/sha → a sha). the container
  # reaches it via /host-repo's shared objects; the host worktree bases its new
  # branch on it. only meaningful at creation — resume reuses the existing
  # workspace, so the two are mutually exclusive (checked in main).
  base_ref: Optional[str] = None
  if spec.into is not None:
    base_ref = _resolve_base_ref(spec.into)
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

  # one session per worktree: refuse if a container is already bound to this
  # workspace's mount. a second concurrent session would share /workspace — and
  # its gitignored token-accounting state — and corrupt it.
  if find_container_id(ContainerWorkspace(spec.name, project).path) is not None:
    log.error(
      'session already active in the container for workspace %r; refusing to start a second',
      spec.name,
    )
    return 1

  # cheap --resume existence guard, mirroring host mode: fail before the
  # container is created. the runner resolves the actual session id from the
  # same dir (derived from its in-container cwd).
  if spec.resume:
    projects_dir = ContainerWorkspace(spec.name, project).claude_projects_dir()
    if _latest_jsonl(projects_dir) is None:
      log.error('no claude session found for %s in %s', spec.name, projects_dir)
      return 1

  if spec.bro is not None:
    # CW_BRO themes the container beyond the runner's own process tree (`cw exec`
    # shells render the bro banner); the runner re-sets it next to claude.
    os.environ['CW_BRO'] = spec.bro

  # scope credentials to the session's bro: `--bro` uses its bro directly; a
  # native session themes as CW_BRO (dive-in sets ppp-dev; a manual `cw ss -c`
  # defaults to it too).
  bro_name = spec.bro
  if bro_name is None:
    bro_name = os.environ.get('CW_BRO', _DEFAULT_CW_BRO)
  scoped = _container_secrets(bro_name, mcp=spec.mcp, bro_mode=spec.bro is not None)
  try:
    secrets = _finalize_secrets(scoped.required, grant=spec.grant, revoke=spec.revoke)
  except ValueError as e:
    log.error('%s', e)
    return 1

  env: dict[str, str] = {}
  if base_ref is not None:
    # the entrypoint reads CW_BASE_REF to base the fresh clone's worktree branch
    # (the sha's objects are already shared from /host-repo via clone alternates)
    env['CW_BASE_REF'] = base_ref
  code = run_in_container(
    spec.name,
    ['cw', *spec.to_in_place_argv()],
    drop=spec.drop,
    secrets=secrets,
    optional_secrets=scoped.optional,
    docker_sock=scoped.docker_sock,
    extra_env=env if len(env) > 0 else None,
  )
  if not spec.drop and code == 0:
    _replace_container_resume_hint(spec.name)
  return code


def _run_host_root_via_broker(
  command: list[str], worktree: Path, project: Path, env: dict[str, str]
) -> int:
  """run the host session as the broker's root peer: provision its channel socket
  under `var/cw/broker`, point `BROKER_CHANNEL` at it in the runner's env, and
  supervise the runner process on the broker loop until it exits."""
  # imported here, not at module level: _broker_enabled() must be able to short-circuit
  # a launch before anything touches the broker package (see its docstring).
  from cw.spawn import ProcessLaunchSpec, ProcessSpawner, run_root_via_broker

  launch = ProcessLaunchSpec(command=command, cwd=str(worktree), env=env)
  return run_root_via_broker(launch, ProcessSpawner(), project)


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
  pidfile = workspace.pidfile
  pidfile.parent.mkdir(parents=True, exist_ok=True)
  pidfile.write_text(str(os.getpid()))
  try:
    if _broker_enabled():
      code = _run_host_root_via_broker(command, worktree, project, runner_env)
    else:
      code = subprocess.run(command, cwd=str(worktree), env=runner_env).returncode
  finally:
    pidfile.unlink(missing_ok=True)

  if spec.drop:
    workspace.remove()
  else:
    _finish_host_worktree(workspace, interactive=not spec.auto and sys.stdin.isatty())
  return code
