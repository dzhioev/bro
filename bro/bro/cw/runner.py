"""the in-place session runner (`cw ss --in-place`).

The inner layer of the launch stack: it assumes its cwd is a prepared workspace
(host worktree or container clone) with the workspace venv active, and owns
everything that runs next to claude — resume resolution, the claude argv, the
session-local MCP server, bro-skill surfacing, CW_SESSION_CONTEXT, and the
post-exit transcript sync for `--bare` sessions. The outer `cw ss` (mode-specific
by nature: worktree ensure / container machinery) validates policy once and
spawns this runner in the workspace, so it re-runs no policy gates.
"""

import contextlib
import os
import signal
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from base import log
from cw.bro import _populate_bro_skills
from cw.broxy import _start_session_broxy
from cw.claude_argv import build_claude_launch
from cw.constants import _BRO_GIT_EMAIL, _BRO_GIT_NAME
from cw.git import git_out
from cw.mcp import _SessionMCPServer, _start_session_mcp_server
from cw.paths import _claude_projects_dir, _in_container, _latest_jsonl
from cw.secrets import _apply_claude_auth
from cw.session_context import (
  CW_SESSION_CONTEXT_ENV,
  build_session_context,
  encode_session_context,
)

if TYPE_CHECKING:
  from cw.session import SessionSpec


def _set_session_context(spec: 'SessionSpec', system_prompt: str, workspace: Path) -> None:
  """capture the session's launch context into CW_SESSION_CONTEXT for the
  session-log sync (set in os.environ: claude's hooks and the runner's own
  `--bare` one-shot both read it). the git base is the workspace's HEAD — for a
  fresh workspace the ref the outer based it on, for a resume the branch tip."""
  try:
    base_sha = git_out('rev-parse', 'HEAD', cwd=str(workspace))
  except subprocess.CalledProcessError:
    base_sha = None
  records = build_session_context(
    system_prompt=system_prompt,
    bro_mode=spec.bro is not None,
    branch=f'worktree-{spec.name}',
    base_sha=base_sha,
    base_ref=spec.into,
    mcp=spec.mcp,
    bro=spec.bro,
    proj_root=workspace,
  )
  os.environ[CW_SESSION_CONTEXT_ENV] = encode_session_context(records)


@contextlib.contextmanager
def _sigterm_forwarded_to(process: subprocess.Popen) -> Generator[None]:
  previous = signal.signal(signal.SIGTERM, lambda signum, frame: process.terminate())
  try:
    yield
  finally:
    signal.signal(signal.SIGTERM, previous)


def _run_claude(argv: list[str], env: dict[str, str]) -> int:
  """spawn claude and wait, forwarding SIGTERM to it — claude's raw-mode TTY
  already absorbs Ctrl-C, but a SIGTERM aimed at the runner (docker stop, kill)
  would otherwise strand claude. the runner keeps waiting after forwarding, so
  the post-exit work still runs."""
  process = subprocess.Popen(['claude', *argv], env=env)
  with _sigterm_forwarded_to(process):
    return process.wait()


def _sync_bare_session_log(name: str, workspace: Path) -> None:
  """one-shot transcript upload for a `--bare` session: minimal mode runs no
  hooks, so the SessionStart/SessionEnd sync pair never fires and the session
  would stay invisible to `sessions` / `rewind`. best-effort — a sync failure
  warns rather than failing session teardown."""
  import sync_session_log

  projects_dir = _claude_projects_dir(workspace)
  if _latest_jsonl(projects_dir) is None:
    return
  try:
    sync_session_log.sync_session_log(workspace=name, projects_dir=projects_dir)
  except Exception as e:
    log.warning('session-log sync for %s failed: %s', name, e)


def run_in_place(spec: 'SessionSpec') -> int:
  workspace = Path.cwd()

  claude_args = list(spec.claude_args)
  if spec.resume:
    projects_dir = _claude_projects_dir(workspace)
    latest = _latest_jsonl(projects_dir)
    if latest is None:
      log.error('no claude session found in %s', projects_dir)
      return 1
    log.info('resuming session %s', latest.stem)
    claude_args = ['--resume', latest.stem, *claude_args]

  if spec.auto:
    os.environ['GIT_AUTHOR_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_AUTHOR_EMAIL'] = _BRO_GIT_EMAIL
    os.environ['GIT_COMMITTER_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_COMMITTER_EMAIL'] = _BRO_GIT_EMAIL

  if spec.bro is not None:
    # CW_BRO themes the session (banner, statusLine); a native themed session
    # inherits it from the outer environment instead (dive-in sets ppp-dev)
    os.environ['CW_BRO'] = spec.bro

  with contextlib.ExitStack() as teardown:
    # host mode launches the session broxy (in a container the entrypoint started
    # one and BROKER_CHANNEL already points at it), rewriting BROKER_CHANNEL
    # before the MCP server and claude inherit the environment. a set
    # BROKER_CHANNEL always names a broxy socket: when the broxy cannot run the
    # channel is unset — the session runs without one — and the launch proceeds.
    upstream = os.environ.get('BROKER_CHANNEL')
    if upstream is not None and not _in_container():
      broxy = _start_session_broxy(upstream, os.environ)
      if broxy is not None:
        teardown.callback(broxy.stop)
        os.environ['BROKER_CHANNEL'] = broxy.address
      else:
        del os.environ['BROKER_CHANNEL']

    # session-local MCP serving, one mechanism for both flavors: OS-assigned port
    # published via a port file, per-session bearer token. the tools serve this
    # workspace's code (the runner's cwd and venv).
    server: Optional[_SessionMCPServer] = None
    mcp_spec: Optional[str] = None
    if spec.bro is not None:
      mcp_spec = f'bro:{spec.bro}'
    elif spec.mcp == 'local':
      mcp_spec = 'flow'
    if mcp_spec is not None:
      try:
        server = _start_session_mcp_server(mcp_spec, workspace, os.environ)
      except RuntimeError as e:
        log.error('%s', e)
        return 1
      teardown.callback(server.stop)

    skills_dir: Optional[Path] = None
    bro_env = os.environ.get('CW_BRO')
    if spec.bro is None and bro_env is not None:
      # native themed session: surface the bro's skills as slash commands from a
      # per-session tmp dir via --add-dir, so concurrent sessions on the same
      # repo don't share `.claude/skills/`. a --bro session reaches its skills
      # through the `bro::skill` MCP tool instead (--bare skips discovery).
      skills_dir = Path(tempfile.mkdtemp(prefix=f'cw-skills-{bro_env}-'))
      _populate_bro_skills(skills_dir, bro_env)

    launch = build_claude_launch(
      spec,
      workspace=workspace,
      claude_args=claude_args,
      endpoint=server.endpoint if server is not None else None,
      skills_dir=skills_dir,
    )
    _set_session_context(spec, launch.system_prompt, workspace)

    if server is not None and spec.bro is not None:
      # gate the launch on full tool readiness: the argv build above overlapped
      # the server's own bro import, so much of the wait is already paid
      try:
        server.wait_healthy()
      except RuntimeError as e:
        log.error('%s', e)
        return 1

    env = {**os.environ}
    _apply_claude_auth(env, warn_when_missing=spec.bro is None)
    code = _run_claude(launch.argv, env)

  if spec.bro is not None:
    _sync_bare_session_log(spec.name, workspace)
  return code
