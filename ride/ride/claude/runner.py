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
from bro.channel import BroChannel
from bro.launch.hold import HOLD_VARIABLE
from bro.monitor import (
  CLAUDE_CONFIG_DIR_ENV,
  SESSION_DIR_ENV,
  claude_projects_dir,
  trail_pointer,
  workspace_session_dir,
)
from bro.summon import SUMMONED_ENV, SUMMONER_ENV
from bro.workspace.git import git_out
from bro.workspace.paths import in_container, workspace_dir
from ride.claude.claude_argv import build_claude_launch
from ride.claude.claude_auth import apply_claude_auth
from ride.claude.claude_config import latest_jsonl, provision_host_claude_dir
from ride.claude.harness import options
from ride.claude.interrupt import run_interactive, run_printing
from ride.claude.mcp import start_session_mcp_server
from ride.claude.recorder import start_session_recorder
from ride.claude.session_context import (
  RIDE_SESSION_CONTEXT_ENV,
  build_session_context,
  encode_session_context,
)
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


def _run_claude(argv: list[str], env: dict[str, str], transcripts: Path) -> int:
  return run_interactive(['claude', *argv], env, transcripts)


# the recorder adopts the transcript and publishes the pointer within its own
# polling cadence, so a tighter poll here would only spin
_TRAIL_POLL_SECONDS = 1.0


def _announce_started(announced: threading.Event, workspace: str) -> None:
  """emit `started{trail_id, workspace}` once, with the trail id the session
  recorder published; no-op when it is not published yet or the session has no
  channel."""
  pointer = trail_pointer.path()
  trail_id = trail_pointer.read(pointer) if pointer is not None else None
  if trail_id is None or announced.is_set():
    return
  channel = BroChannel.from_env()
  if channel is not None:
    channel.started(trail_id, workspace=workspace)
    channel.close()
  announced.set()


@contextlib.contextmanager
def _started_watch(workspace: str) -> Generator[threading.Event]:
  """watch for the session recorder's current-trail pointer for the block's
  duration and announce `started` when it lands, yielding the announced event."""
  announced = threading.Event()
  stop = threading.Event()

  def _watch() -> None:
    while not stop.wait(_TRAIL_POLL_SECONDS):
      _announce_started(announced, workspace)
      if announced.is_set():
        return

  thread = threading.Thread(target=_watch, daemon=True)
  thread.start()
  try:
    yield announced
  finally:
    stop.set()
    thread.join()


def _run_claude_summoned(argv: list[str], env: dict[str, str], workspace: str) -> int:
  """the `_run_claude` of a summoned solo child: claude runs in print mode with
  its stdout captured, and the runner emits the run lifecycle a bro-run child
  gets from `BaseBro.run` — `started{trail_id, workspace}` once the session
  recorder publishes the current-trail pointer, and `completed{result,
  end_reason: ok}` carrying the printed reply after a clean exit. A non-zero
  exit or a stopped run emits no terminal: the broker synthesizes `failed{exit,
  output_tail}` for the former, and a `raise`- or `answer`-ended session already
  sent its own `completed`. The captured reply is echoed to stdout either way,
  so the child's output tail still carries it."""
  with _started_watch(workspace) as announced:
    run = run_printing(['claude', *argv], env)
  print(run.output, end='', flush=True)
  if run.code != 0 or run.stopped:
    return run.code
  # a run short enough to end inside the recorder's adoption cadence announces
  # here or not at all; the terminal must not wait on recording
  _announce_started(announced, workspace)
  channel = BroChannel.from_env()
  if channel is not None:
    channel.completed(run.output.rstrip('\n'), 'ok')
    channel.close()
  return run.code


def _run_claude_summoned_interactive(
  argv: list[str], env: dict[str, str], workspace: str, transcripts: Path
) -> int:
  """the `_run_claude` of a manual summon child: claude runs interactively as
  usual, and the runner only announces `started{trail_id, workspace}`. The
  terminal is the `answer` service tool's own `completed` — a session that ends
  without it produced no answer, which the broker turns into the summoner's
  synthesized failure when the channel goes."""
  with _started_watch(workspace):
    return _run_claude(argv, env, transcripts)


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

  # hold and kill wiring for the `raise` service tool's mounts (bro/bro.py).
  # both overwrite any ambient value: a session launched from inside another
  # must not inherit its hold or kill target.
  os.environ[HOLD_VARIABLE] = spec.hold
  os.environ['RIDE_RUNNER_PID'] = str(os.getpid())

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
      recorder = start_session_recorder(spec.name, tree, os.environ, llm=spec.llm_spec.dump())
      if recorder is not None:
        teardown.callback(recorder.stop)
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
    apply_claude_auth(env, warn_when_missing=not options(spec).raw)
    log.info('launching claude')
    if os.environ.get(SUMMONED_ENV) is None:
      code = _run_claude(launch.argv, env, transcripts)
    elif spec.solo:
      code = _run_claude_summoned(launch.argv, env, spec.name)
    else:
      code = _run_claude_summoned_interactive(launch.argv, env, spec.name, transcripts)

  return code
