"""the in-place Claude session runner (`ride solo|along --in-place`).

The inner layer of the launch stack: it assumes its cwd is a prepared workspace
tree (host worktree or container clone) under the session runtime environment,
and owns everything that runs next to claude — resume resolution, the claude argv, the
session-local MCP server, launch declarations, and the session recorder daemon. The outer `ride solo|along` (mode-specific by nature: worktree
ensure / container machinery) validates policy once and spawns this runner in
the workspace, so it re-runs no policy gates.
"""

import contextlib
import os
import subprocess
import threading
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from bro.base import log
from bro.monitor import (
  CLAUDE_CONFIG_DIR_ENV,
  SESSION_DIR_ENV,
  claude_projects_dir,
  trail_pointer,
  workspace_session_dir,
)
from bro.run_lifecycle import RunLifecycle
from bro.summon import SUMMONER_ENV, summoned
from bro.workspace.git import git_out
from bro.workspace.paths import in_container, workspace_dir
from ride.claude.claude_argv import build_claude_launch
from ride.claude.claude_auth import apply_claude_auth
from ride.claude.claude_config import latest_jsonl, provision_host_claude_dir
from ride.claude.harness import options
from ride.claude.interrupt import InteractiveRun, run_interactive, run_printing
from ride.claude.mcp import start_session_mcp_server
from ride.claude.recorder import start_session_recorder
from ride.claude.session_context import (
  RIDE_SESSION_CONTEXT_ENV,
  build_session_context,
  encode_session_context,
)
from ride.claude.statusline import start_statusline_projector
from ride.repository import is_git_url

if TYPE_CHECKING:
  from ride.session import SessionSpec


def _set_session_context(spec: 'SessionSpec', system_prompt: str, tree: Path) -> None:
  """capture the session's launch context into RIDE_SESSION_CONTEXT for the
  session recorder daemon (set in os.environ, which the daemon's spawn
  snapshots). the git base is the tree's HEAD — for a fresh workspace the
  ref the outer based it on, for a resume the branch tip."""
  try:
    base_sha = git_out('rev-parse', 'HEAD', cwd=str(tree))
  except subprocess.CalledProcessError:
    base_sha = None
  records = build_session_context(
    system_prompt=system_prompt,
    branch=f'worktree-{spec.name}' if spec.repo is not None else None,
    base_sha=base_sha,
    base_ref=spec.into,
    bro=spec.bro,
    raw=options(spec).raw,
    proj_root=tree,
  )
  os.environ[RIDE_SESSION_CONTEXT_ENV] = encode_session_context(records)


def _run_claude(argv: list[str], env: dict[str, str], transcripts: Path) -> InteractiveRun:
  return run_interactive(['claude', *argv], env, transcripts)


# the recorder adopts the transcript and publishes the pointer within its own
# polling cadence, so a tighter poll here would only spin
_TRAIL_POLL_SECONDS = 1.0


def _announce_trail(emitted: threading.Event) -> None:
  """emit the trail mark once, with the trail id the session recorder
  published; no-op when it is not published yet or the session has no
  channel."""
  pointer = trail_pointer.path()
  trail_id = trail_pointer.read(pointer) if pointer is not None else None
  if trail_id is None or emitted.is_set():
    return
  channel = RunLifecycle.from_env()
  if channel is not None:
    channel.trail(trail_id)
    channel.close()
  emitted.set()


@contextlib.contextmanager
def _trail_watch() -> Generator[threading.Event]:
  """Watch for the recorder's current-trail pointer and emit its trail mark."""
  emitted = threading.Event()
  stop = threading.Event()

  def _watch() -> None:
    while not stop.wait(_TRAIL_POLL_SECONDS):
      _announce_trail(emitted)
      if emitted.is_set():
        return

  thread = threading.Thread(target=_watch, daemon=True)
  thread.start()
  try:
    yield emitted
  finally:
    stop.set()
    thread.join()


def _complete_run(emitted: threading.Event, result: str | None) -> None:
  # a run short enough to end inside the recorder's adoption cadence announces
  # here or not at all; the result must not wait on recording
  _announce_trail(emitted)
  channel = RunLifecycle.from_env()
  if channel is not None:
    pointer = trail_pointer.path()
    trail_id = trail_pointer.read(pointer) if pointer is not None else None
    channel.completed(result, 'ok', trail_id=trail_id)
    channel.close()


def _run_claude_root_solo(argv: list[str], env: dict[str, str], transcripts: Path) -> int:
  """run a root's print-mode Claude and close its host-anchored quest on success."""
  with _trail_watch() as emitted:
    run = _run_claude(argv, env, transcripts)
  if run.code == 0 and not run.stopped:
    _complete_run(emitted, None)
  return run.code


def _run_claude_summoned(argv: list[str], env: dict[str, str]) -> int:
  """run a summoned print-mode Claude and emit its broker lifecycle.

  A clean exit answers with the printed reply. A non-zero exit or a stopped run
  emits no result: the broker synthesizes `result{failed}` from reap for the
  former, and a `raise`- or `answer`-ended session already sent its own. The
  captured reply is echoed to stdout either way, so the output tail still
  carries it."""
  with _trail_watch() as emitted:
    run = run_printing(['claude', *argv], env)
  print(run.output, end='', flush=True)
  if run.code != 0 or run.stopped:
    return run.code
  _complete_run(emitted, run.output.rstrip('\n'))
  return run.code


def _run_claude_summoned_interactive(
  argv: list[str], env: dict[str, str], transcripts: Path
) -> int:
  """the `_run_claude` of a manual summon child: claude runs interactively as
  usual, and the runner only emits the trail mark. The result is the
  `answer` service tool's own — a session that ends without it produced no
  answer, which the broker turns into the summoner's synthesized failure when
  the channel goes."""
  with _trail_watch():
    return _run_claude(argv, env, transcripts).code


def run_in_place(spec: 'SessionSpec') -> int:
  tree = Path.cwd()

  if not in_container():
    # a host session runs claude against the workspace's own claude state, the
    # container-equivalent isolation (reference/ride.md, "Host claude-state
    # isolation"). provisioning is idempotent because both launch layers apply it.
    # Set before anything derives paths or spawns children:
    # the resume resolution below, the hooks, and claude itself all read it.
    workspace_path = workspace_dir(spec.name)
    if spec.repo is None:
      project = tree
    elif not is_git_url(spec.repo):
      project = Path(spec.repo)
    else:
      common = Path(git_out('rev-parse', '--git-common-dir', cwd=str(tree)))
      common = common if common.is_absolute() else (tree / common).resolve()
      project = common.parent if common.name == '.git' else common
    claude_dir = provision_host_claude_dir(workspace_path, tree, project)
    os.environ[CLAUDE_CONFIG_DIR_ENV] = str(claude_dir)
    os.environ[SESSION_DIR_ENV] = str(workspace_session_dir(workspace_path))

  transcripts = claude_projects_dir(tree)
  claude_args = list(spec.arguments)
  if spec.resume:
    latest = latest_jsonl(transcripts)
    if latest is None:
      log.error('no claude session found in %s', transcripts)
      return 1
    log.info('resuming session %s', latest.stem)
    claude_args = ['--resume', latest.stem, *claude_args]

  with contextlib.ExitStack() as teardown:
    # session-local MCP serving, one mechanism for both flavors: OS-assigned port
    # published via a port file, per-session bearer token. the server imports from
    # the session runtime selected by PATH — the snapshot on host, the runtime
    # volume in a container.
    if options(spec).raw:
      mcp_spec = f'bro:{spec.bro}'
    else:
      mcp_spec = f'persona:{spec.bro}'
    try:
      server = start_session_mcp_server(mcp_spec, tree, os.environ)
    except RuntimeError as error:
      log.error('%s', error)
      return 1
    teardown.callback(server.stop)

    launch = build_claude_launch(spec, claude_args=claude_args, endpoint=server.endpoint)
    _set_session_context(spec, launch.system_prompt, tree)

    # after the session context: the daemon's spawn snapshots os.environ, and
    # RIDE_SESSION_CONTEXT becomes the trail's launch-context attachment
    if os.environ.get('TRAILS_DISABLED') is None:
      try:
        recorder = start_session_recorder(spec.name, tree, os.environ, llm=spec.llm_spec.dump())
      except RuntimeError as error:
        log.error('%s', error)
        return 1
      teardown.callback(recorder.stop)
    try:
      statusline_projector = start_statusline_projector(os.environ)
    except RuntimeError as error:
      log.warning('%s', error)
    else:
      teardown.callback(statusline_projector.stop)

    # the recorder above got its copy; claude's subprocesses must not see the
    # summoner attribution, or a nested in-place run would stamp it on its own
    # trail (bro.summon.summoned_by_from_env owns the semantics)
    os.environ.pop(SUMMONER_ENV, None)

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
    raw = options(spec).raw
    if not raw:
      # claude resolves fast-mode availability from a stored OAuth credentials
      # file, and left to guess without one reports it disabled by an organization
      env['CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK'] = '1'
    apply_claude_auth(env, warn_when_missing=not raw)
    log.info('launching claude')
    if spec.solo and summoned():
      code = _run_claude_summoned(launch.argv, env)
    elif spec.solo:
      code = _run_claude_root_solo(launch.argv, env, transcripts)
    elif summoned():
      code = _run_claude_summoned_interactive(launch.argv, env, transcripts)
    else:
      code = _run_claude(launch.argv, env, transcripts).code

  return code
