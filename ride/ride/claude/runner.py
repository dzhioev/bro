"""the in-place Claude session runner (`ride solo|along --in-place`).

The inner layer of the launch stack: it assumes its cwd is a prepared workspace
(host worktree or container clone) with the workspace venv active, and owns
everything that runs next to claude — resume resolution, the claude argv, the
session-local MCP server, launch declarations, and the session recorder daemon. The outer `ride solo|along` (mode-specific by nature: worktree
ensure / container machinery) validates policy once and spawns this runner in
the workspace, so it re-runs no policy gates.
"""

import contextlib
import os
import signal
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from bro.base import log
from bro.launch.broxy import _start_session_broxy
from bro.launch.identity import bro_git_identity_env
from bro.monitor import claude_projects_dir
from bro.workspace.git import git_out
from bro.workspace.paths import in_container, project_root
from ride.claude.claude_argv import build_claude_launch
from ride.claude.claude_auth import _apply_claude_auth
from ride.claude.claude_config import _latest_jsonl, _provision_host_claude_dir
from ride.claude.harness import options
from ride.claude.mcp import _start_session_mcp_server
from ride.claude.recorder import _start_session_recorder
from ride.claude.session_context import (
  CW_SESSION_CONTEXT_ENV,
  build_session_context,
  encode_session_context,
)

if TYPE_CHECKING:
  from ride.session import SessionSpec


def _set_session_context(spec: 'SessionSpec', system_prompt: str, workspace: Path) -> None:
  """capture the session's launch context into CW_SESSION_CONTEXT for the
  session recorder daemon (set in os.environ, which the daemon's spawn
  snapshots). the git base is the workspace's HEAD — for a fresh workspace the
  ref the outer based it on, for a resume the branch tip."""
  try:
    base_sha = git_out('rev-parse', 'HEAD', cwd=str(workspace))
  except subprocess.CalledProcessError:
    base_sha = None
  records = build_session_context(
    system_prompt=system_prompt,
    branch=f'worktree-{spec.name}',
    base_sha=base_sha,
    base_ref=spec.into,
    bro=spec.session_bro,
    raw=options(spec).raw,
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
  already absorbs Ctrl-C, but a SIGTERM aimed at the runner (docker stop, kill,
  terminate_session) would otherwise strand claude. the runner keeps waiting
  after forwarding, so the post-exit work still runs."""
  process = subprocess.Popen(['claude', *argv], env=env)
  with _sigterm_forwarded_to(process):
    return process.wait()


def run_in_place(spec: 'SessionSpec') -> int:
  workspace = Path.cwd()

  if not in_container():
    # a host session's claude state lives in the private per-session config dir
    # (the container-equivalent isolation — reference/ride.md, "Host claude-state
    # isolation"). provisioning is idempotent and the outer launch also applies
    # it, so a runner spawned by an outer cw that predates the config dir still
    # provisions its own. set before anything derives paths or spawns children:
    # the resume resolution below, the hooks, and claude itself all read it.
    claude_dir = _provision_host_claude_dir(spec.name, workspace, project_root())
    os.environ['CLAUDE_CONFIG_DIR'] = str(claude_dir)

  claude_args = list(options(spec).arguments)
  if spec.resume:
    projects_dir = claude_projects_dir(workspace)
    latest = _latest_jsonl(projects_dir)
    if latest is None:
      log.error('no claude session found in %s', projects_dir)
      return 1
    log.info('resuming session %s', latest.stem)
    claude_args = ['--resume', latest.stem, *claude_args]

  os.environ.update(bro_git_identity_env(spec.session_bro))

  # CW_BRO themes the session (banner, statusLine)
  os.environ['CW_BRO'] = spec.session_bro

  # feature-declared workspace provisioning (bro/bro.py's _provision_workspace
  # is the bro-harness counterpart): a commit-accounting persona gets the
  # footer hooks installed into the session workspace, so agent commits carry
  # the token footer with no session involvement. hooks already present are
  # left alone.
  from bro.registry import create_bro

  if create_bro(spec.session_bro).has_feature('commit-accounting'):
    from bro.workflow.commit_footer import install_hooks

    install_hooks(workspace, overwrite=False)

  # hold and kill wiring for the `raise` service tool's mounts (bro/bro.py).
  # both overwrite any ambient value: a session launched from inside another
  # must not inherit its hold or kill target.
  os.environ['BRO_HOLD'] = spec.hold
  os.environ['CW_RUNNER_PID'] = str(os.getpid())

  with contextlib.ExitStack() as teardown:
    # host mode launches the session broxy (in a container the entrypoint started
    # one and BROKER_CHANNEL already points at it), rewriting BROKER_CHANNEL
    # before the MCP server and claude inherit the environment. a set
    # BROKER_CHANNEL always names a broxy socket: when the broxy cannot run the
    # channel is unset — the session runs without one — and the launch proceeds.
    upstream = os.environ.get('BROKER_CHANNEL')
    if upstream is not None and not in_container():
      broxy = _start_session_broxy(upstream, os.environ)
      if broxy is not None:
        teardown.callback(broxy.stop)
        os.environ['BROKER_CHANNEL'] = broxy.address
      else:
        del os.environ['BROKER_CHANNEL']

    # session-local MCP serving, one mechanism for both flavors: OS-assigned port
    # published via a port file, per-session bearer token. the tools serve this
    # workspace's code (the runner's cwd and venv) — the bro's own toolset under
    # --raw, the persona's claude-harness namespaces for a cw-session.
    if options(spec).raw:
      mcp_spec = f'bro:{spec.session_bro}'
    else:
      mcp_spec = f'persona:{spec.session_bro}'
    try:
      server = _start_session_mcp_server(mcp_spec, workspace, os.environ)
    except RuntimeError as error:
      log.error('%s', error)
      return 1
    teardown.callback(server.stop)

    launch = build_claude_launch(spec, claude_args=claude_args, endpoint=server.endpoint)
    _set_session_context(spec, launch.system_prompt, workspace)

    # after the session context: the daemon's spawn snapshots os.environ, and
    # CW_SESSION_CONTEXT becomes the trail's launch-context attachment
    recorder = _start_session_recorder(spec.name, workspace, os.environ, llm=spec.llm_spec.dump())
    if recorder is not None:
      teardown.callback(recorder.stop)

    # gate the launch on full tool readiness: the argv build above overlapped
    # the server's own bro import, so much of the wait is already paid
    try:
      server.wait_healthy()
    except RuntimeError as error:
      log.error('%s', error)
      return 1
    log.verbose('session MCP server healthy')

    env = {**os.environ}
    # claude's MCP tool-call timeout (ms): the ~1-minute default kills
    # legitimately slow tools (vision audits, renders)
    env['MCP_TOOL_TIMEOUT'] = str(10 * 60 * 1000)
    _apply_claude_auth(env, warn_when_missing=not options(spec).raw)
    log.info('launching claude')
    code = _run_claude(launch.argv, env)

  return code
