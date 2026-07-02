import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from base import credentials, log
from cw.bro import _bro_launch, _populate_bro_skills
from cw.constants import _CW_MODEL
from cw.containers import _replace_container_resume_hint, run_in_container
from cw.git import git_out
from cw.mcp import _container_mcp_launch, _HostMCPServer, _start_host_mcp_server
from cw.paths import _latest_jsonl, _project_root, _venv_env
from cw.secrets import (
  _DEFAULT_CW_BRO,
  _claude_code_token_env,
  _container_secrets,
  _finalize_secrets,
)
from cw.session_context import (
  CW_SESSION_CONTEXT_ENV,
  build_session_context,
  encode_session_context,
)
from cw.system_prompt import _session_append_prompt
from cw.workspace import ContainerWorkspace, HostWorktree
from cw.worktrees import _ensure_host_worktree, _finish_host_worktree, _provision_host_worktree

_BRO_GIT_NAME = 'Bro'
_BRO_GIT_EMAIL = 'dzhioev+bro@gmail.com'


def _deployed_mcp_argv() -> list[str]:
  """`--mcp-config` argv pointing at the deployed flow MCP server (`--mcp http`)."""
  try:
    cfg = credentials.get_json('flow_mcp')
  except credentials.SecretNotFound:
    raise SystemExit('missing flow_mcp secret — run flow/mcp/server/bootstrap_secrets.sh')
  mcp_json = json.dumps(
    {
      'mcpServers': {
        'flow': {
          'type': 'http',
          'url': cfg['url'],
          'headers': {'Authorization': f'Bearer {cfg["token"]}'},
        },
      },
    },
    separators=(',', ':'),
  )
  return ['--mcp-config', mcp_json]


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


def _set_session_context(
  spec: 'SessionSpec', system_prompt: str, *, bro_mode: bool, resolved_base: Optional[str]
) -> None:
  """capture the session's launch context into CW_SESSION_CONTEXT for sync-session-log.

  `resolved_base` is the --into ref resolved to a sha (None when --into is unset),
  used as the git base_sha; without --into the base is the project HEAD at launch.
  """
  proj = _project_root()
  base_sha = resolved_base
  if base_sha is None:
    try:
      base_sha = git_out('rev-parse', 'HEAD', cwd=str(proj))
    except subprocess.CalledProcessError:
      base_sha = None
  records = build_session_context(
    system_prompt=system_prompt,
    bro_mode=bro_mode,
    branch=f'worktree-{spec.name}',
    base_sha=base_sha,
    base_ref=spec.into,
    mcp=spec.mcp,
    bro=spec.bro,
    proj_root=proj,
  )
  os.environ[CW_SESSION_CONTEXT_ENV] = encode_session_context(records)


@dataclass(frozen=True)
class SessionSpec:
  """the parameters of a `cw ss` session, as parsed from its argv.

  one object replaces the positional soup threaded through start_session → cw;
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
      parts.append('--mcp')
      if self.mcp != 'http':
        parts.append(self.mcp)
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

  claude_args = list(spec.claude_args)

  if spec.resume:
    proj = _project_root()
    ws = ContainerWorkspace(spec.name, proj) if spec.container else HostWorktree(spec.name, proj)
    projects_dir = ws.claude_projects_dir()
    latest = _latest_jsonl(projects_dir)
    if latest is None:
      log.error('no claude session found for %s in %s', spec.name, projects_dir)
      return 1
    session_id = latest.stem
    log.info('resuming session %s', session_id)
    claude_args = ['--resume', session_id, *claude_args]

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

  if spec.auto:
    os.environ['GIT_AUTHOR_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_AUTHOR_EMAIL'] = _BRO_GIT_EMAIL
    os.environ['GIT_COMMITTER_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_COMMITTER_EMAIL'] = _BRO_GIT_EMAIL

  if spec.bro is not None:
    # CW_BRO themes the container session (banner, statusLine). the bro's skills
    # reach a `--bro` session through its `skill` MCP tool (served by the
    # session-local bro MCP server), not `.claude/skills/` slash commands —
    # `claude --bare` skips skills auto-discovery. host-mode `--bro` is
    # unsupported (the --bare flow needs the container entrypoint to start that
    # server and wire the api-key helper).
    os.environ['CW_BRO'] = spec.bro
    launch = _bro_launch(spec.bro)
    bro_argv = launch.claude_argv
    _set_session_context(
      spec,
      bro_argv[bro_argv.index('--system-prompt') + 1],
      bro_mode=True,
      resolved_base=base_ref,
    )
    claude_args = [*bro_argv, *claude_args]
    if spec.effort is not None:
      claude_args = ['--effort', spec.effort, *claude_args]
    if spec.prompt is not None:
      claude_args = [*claude_args, '--', spec.prompt]
    scoped = _container_secrets(spec.bro, mcp=spec.mcp, bro_mode=True)
    try:
      required = _finalize_secrets(scoped.required, grant=spec.grant, revoke=spec.revoke)
    except ValueError as e:
      log.error('%s', e)
      return 1
    return cw(
      spec,
      base_ref=base_ref,
      claude_args=claude_args,
      secrets=required,
      optional_secrets=scoped.optional,
      docker_sock=scoped.docker_sock,
      extra_env=launch.extra_env,
    )

  fast_mode_settings = json.dumps({'fastMode': spec.fast})
  inject = [
    '--model',
    _CW_MODEL,
    '--disallowed-tools',
    'mcp__claude_ai_*',
    '--settings',
    fast_mode_settings,
  ]
  if spec.effort is not None:
    inject.extend(['--effort', spec.effort])
  # --mcp local is wired in cw(), not here: it depends on the final
  # host/container decision (the in-container fallback) and, on the host path,
  # on the provisioned worktree the server runs from.
  if spec.mcp == 'http':
    inject.extend(_deployed_mcp_argv())
  if spec.auto:
    inject.append('--dangerously-skip-permissions')
  claude_args = [*inject, *claude_args]

  bro_env = os.environ.get('CW_BRO')
  append_prompt = _session_append_prompt(spec.auto, bro_env)
  claude_args = [*claude_args, '--append-system-prompt', append_prompt]
  _set_session_context(spec, append_prompt, bro_mode=False, resolved_base=base_ref)

  # host-mode bro skill surfacing: populate a per-session tmp dir and pass it
  # via `--add-dir` so claude's skill discovery picks up `<dir>/.claude/skills/`.
  # avoids the shared `<proj>/.claude/skills/` collision when multiple host
  # sessions run on the same repo — each `_populate_bro_skills` call wipes
  # foreign symlinks before recreating its own, which previously trampled
  # concurrent sessions. container mode keeps writing to the workspace's
  # `.claude/skills/` (single-session FS, no concurrency).
  if not spec.container and bro_env is not None:
    skills_root = Path(tempfile.mkdtemp(prefix=f'cw-skills-{bro_env}-'))
    _populate_bro_skills(skills_root, bro_env)
    claude_args = [*claude_args, '--add-dir', str(skills_root)]

  if spec.prompt is not None:
    claude_args = [*claude_args, '--', spec.prompt]

  # scope credentials to the themed bro (dive-in sets CW_BRO=ppp-dev; a manual
  # `cw ss -c` defaults to it too). host mode resolves from ~/.ppp directly, so no
  # hydration there.
  secrets: set[str] = set()
  optional: set[str] = set()
  if spec.container:
    bro_name = bro_env if bro_env is not None else _DEFAULT_CW_BRO
    scoped = _container_secrets(bro_name, mcp=spec.mcp, bro_mode=False)
    optional = scoped.optional
    try:
      secrets = _finalize_secrets(scoped.required, grant=spec.grant, revoke=spec.revoke)
    except ValueError as e:
      log.error('%s', e)
      return 1
  return cw(
    spec,
    base_ref=base_ref,
    claude_args=claude_args,
    secrets=secrets,
    optional_secrets=optional,
  )


def cw(
  spec: SessionSpec,
  *,
  base_ref: Optional[str] = None,
  claude_args: list[str],
  secrets: Collection[str] = (),
  optional_secrets: Collection[str] = (),
  docker_sock: bool = True,
  extra_env: Optional[Mapping[str, str]] = None,
) -> int:
  container = spec.container
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    if spec.bro is not None:
      # a --bro session depends on the container entrypoint (bro MCP server
      # start + apiKeyHelper wiring), so it cannot degrade to host mode
      log.error('--bro sessions cannot nest inside a container')
      return 1
    log.info('already inside a container; falling back to host mode')
    container = False

  if container:
    env = dict(extra_env) if extra_env is not None else {}
    if spec.mcp == 'local':
      # session-local flow MCP server: the entrypoint reads CW_MCP_HTTP_* to
      # start `mcp-server flow --http` and gates the claude exec on its bind
      mcp_env, mcp_config = _container_mcp_launch('flow', ['flow'])
      env.update(mcp_env)
      claude_args = [*claude_args, '--mcp-config', mcp_config]
    if base_ref is not None:
      # the entrypoint reads CW_BASE_REF to base the fresh clone's worktree branch
      # (the sha's objects are already shared from /host-repo via clone alternates)
      env['CW_BASE_REF'] = base_ref
    code = run_in_container(
      spec.name,
      ['claude', *claude_args],
      drop=spec.drop,
      secrets=secrets,
      optional_secrets=optional_secrets,
      docker_sock=docker_sock,
      extra_env=env if len(env) > 0 else None,
    )
    if not spec.drop and code == 0:
      _replace_container_resume_hint(spec.name)
    return code

  # host mode: cw owns the worktree lifecycle (create + provision + cleanup) and
  # launches plain `claude` from inside it — no `claude -w`, so no claude provisioning
  # hooks. provisioning is the same provision_repo.sh the container entrypoint runs.
  proj = _project_root()
  os.chdir(proj)
  ws = HostWorktree(spec.name, proj)
  worktree = ws.path
  branch = f'worktree-{spec.name}'

  if not _ensure_host_worktree(worktree, branch, base_ref):
    return 1
  if not _provision_host_worktree(worktree):
    return 1

  env = _venv_env(worktree / '.venv')
  env.update(_claude_code_token_env())

  # session-local flow MCP server on an OS-assigned port (the shared host netns
  # rules out the container's fixed one), started from the provisioned worktree
  # so "local" means this checkout's flow code. terminated when claude exits;
  # a SIGKILLed cw orphans it (no watchdog).
  mcp_server: Optional[_HostMCPServer] = None
  if spec.mcp == 'local':
    try:
      mcp_server = _start_host_mcp_server(worktree, env)
    except RuntimeError as e:
      log.error('%s', e)
      return 1
    claude_args = [*claude_args, '--mcp-config', mcp_server.mcp_config()]

  pidfile = ws.pidfile
  pidfile.parent.mkdir(parents=True, exist_ok=True)
  pidfile.write_text(str(os.getpid()))
  try:
    result = subprocess.run(['claude', *claude_args], cwd=str(worktree), env=env)
  finally:
    pidfile.unlink(missing_ok=True)
    if mcp_server is not None:
      mcp_server.stop()

  if spec.drop:
    ws.remove()
  else:
    _finish_host_worktree(ws, interactive=not spec.auto and sys.stdin.isatty())
  return result.returncode
