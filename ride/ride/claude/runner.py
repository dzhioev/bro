"""The Claude harness runner under `do-ride`.

It assumes its cwd is a prepared workspace tree under the session runtime environment.
It owns everything that runs next to Claude:
resume resolution, argv, the session-local MCP server, launch declarations, and the recorder daemon.
The outer `ride solo|along` validates policy once, so this runner repeats no policy gates.
"""

import contextlib
import os
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro import brash_policy, watches
from bro.base import log
from bro.monitor import (
  SESSION_DIR_ENV,
  claude_config_dir,
  claude_projects_dir,
  harness_session_dir,
  trail_pointer,
)
from bro.run_lifecycle import RunLifecycle
from bro.summon import RUNTIME_ENV, SUMMONER_ENV, summoned
from bro.workspace.paths import ISOLATION_ENV
from ride.claude import claude_release
from ride.claude.claude_argv import build_claude_launch
from ride.claude.claude_auth import apply_claude_auth
from ride.claude.claude_config import latest_jsonl
from ride.claude.competing_hooks import find as find_competing_hooks
from ride.claude.interrupt import Run, StreamedRun, run_interactive, run_streaming
from ride.claude.mcp import start_session_mcp_server
from ride.claude.recorder import start_session_recorder
from ride.claude.shell_prefix import apply_shell_prefix
from ride.claude.statusline import start_statusline_projector
from ride.claude.waiter_state import WaiterState
from ride.workspace.build_context import claude_code_version

if TYPE_CHECKING:
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


_CONTAINER_CLAUDE = Path('/opt/claude-code/claude')
_MCP_BACKSTOP_MILLISECONDS = 24 * 60 * 60 * 1000


def _claude_binary() -> Path:
  isolation = os.environ.get(ISOLATION_ENV)
  if isolation == 'boxed':
    return _CONTAINER_CLAUDE
  if isolation != 'unboxed':
    raise RuntimeError(f'{ISOLATION_ENV} must be set to boxed or unboxed')

  runtime_value = os.environ.get(RUNTIME_ENV)
  if runtime_value is None:
    raise RuntimeError(f'{RUNTIME_ENV} is unset')
  runtime = Path(runtime_value)
  if not runtime.is_absolute():
    raise RuntimeError(f'{RUNTIME_ENV} must be an absolute path, not {runtime_value!r}')
  carried_directory = runtime / 'claude'
  if carried_directory.exists():
    return claude_release.verified_binary(carried_directory / 'claude')
  return claude_release.cached_binary(claude_code_version(), claude_release.host_platform())


def _apply_mcp_backstops(environment: dict[str, str]) -> None:
  value = str(_MCP_BACKSTOP_MILLISECONDS)
  environment['CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT'] = value
  environment['MCP_TOOL_TIMEOUT'] = value


def _run_claude(binary: Path, argv: list[str], env: dict[str, str], transcripts: Path) -> Run:
  return run_interactive([str(binary), *argv], env, transcripts, waiters=WaiterState.for_session())


def _stream_claude(
  binary: Path,
  argv: list[str],
  env: dict[str, str],
  prompt: str,
  on_result: Callable[[str], None] | None = None,
) -> StreamedRun:
  return run_streaming(
    [str(binary), *argv],
    env,
    prompt,
    store=watches.session_store(),
    waiters=WaiterState.for_session(),
    on_result=on_result,
  )


def _claude_state_dir() -> Path:
  session_state = harness_session_dir('claude')
  if session_state is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset')
  return session_state


def _claude_temp_dir() -> Path:
  temp_dir = _claude_state_dir() / 'tmp'
  temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
  return temp_dir


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


def _run_claude_root_solo(binary: Path, argv: list[str], env: dict[str, str], prompt: str) -> int:
  """run a root's print-mode Claude with each turn's reply on the session's
  stdout, and close its host-anchored quest on success."""
  with _trail_watch() as emitted:
    run = _stream_claude(binary, argv, env, prompt, lambda reply: print(reply, flush=True))
  if run.code == 0 and not run.stopped:
    _complete_run(emitted, None)
  return run.code


def _run_claude_summoned(binary: Path, argv: list[str], env: dict[str, str], prompt: str) -> int:
  """run a summoned print-mode Claude and emit its broker lifecycle.

  A clean exit answers with the last turn's reply. A non-zero exit or a stopped
  run emits no result: the broker synthesizes `result{failed}` from reap for the
  former, and a `raise`- or `answer`-ended session already sent its own. The
  reply is echoed to stdout either way, so the output tail still carries it."""
  with _trail_watch() as emitted:
    run = _stream_claude(binary, argv, env, prompt)
  reply = run.results[-1] if len(run.results) > 0 else ''
  print(reply, flush=True)
  if run.code != 0 or run.stopped:
    return run.code
  _complete_run(emitted, reply)
  return run.code


def _run_claude_summoned_interactive(
  binary: Path, argv: list[str], env: dict[str, str], transcripts: Path
) -> int:
  """the `_run_claude` of a manual summon child: claude runs interactively as
  usual, and the runner only emits the trail mark. The result is the
  `answer` service tool's own — a session that ends without it produced no
  answer, which the broker turns into the summoner's synthesized failure when
  the channel goes."""
  with _trail_watch():
    return _run_claude(binary, argv, env, transcripts).code


class _CompetingHooks(RuntimeError):
  """a session's own Claude configuration could take a call around the command gate."""


def _session_brash_policy(bro: str, tree: Path) -> Optional[Path]:
  """write the session's brash policy where `bro`'s command list is finite, and
  return it; None where the bro runs no line in brash. Raises `_CompetingHooks`
  first where the session's own Claude configuration could take a call around
  the command gate."""
  from bro.registry import create_bro

  reach = create_bro(bro).reach()
  if not brash_policy.finite(reach):
    return None
  competing = find_competing_hooks(tree, claude_config_dir())
  if len(competing) > 0:
    raise _CompetingHooks(
      f"{bro}'s finite command list runs each Bash and Monitor line in brash, and this "
      "session's Claude configuration could take a call around that gate:\n  "
      + '\n  '.join(competing)
    )
  state = _claude_state_dir()
  state.mkdir(parents=True, exist_ok=True)
  return brash_policy.write(state, reach)


def run_session(spec: 'SessionSpec | SessionRun') -> int:
  tree = Path.cwd()
  try:
    binary = _claude_binary()
  except (OSError, RuntimeError, ValueError) as error:
    log.error('cannot prepare pinned Claude Code: %s', error)
    return 1
  try:
    policy = _session_brash_policy(spec.bro, tree)
  except _CompetingHooks as error:
    log.error('refusing to start: %s', error)
    return 1

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
    waiters = WaiterState.for_session()
    waiters.reset()
    teardown.callback(waiters.stand_down)
    # session-local MCP serving: OS-assigned port published via a port file,
    # per-session bearer token. the server imports from the session runtime
    # selected by PATH — the snapshot on host, the runtime volume in a container.
    server_env = {
      name: value for name, value in os.environ.items() if name != brash_policy.POLICY_ENV
    }
    if policy is not None:
      server_env[brash_policy.POLICY_ENV] = str(policy)
    try:
      server = start_session_mcp_server(f'persona:{spec.bro}', tree, server_env)
    except RuntimeError as error:
      log.error('%s', error)
      return 1
    teardown.callback(server.stop)

    launch = build_claude_launch(
      spec, claude_args=claude_args, endpoint=server.endpoint, brash_policy=policy
    )
    if os.environ.get('TRAILS_DISABLED') is None:
      try:
        recorder = start_session_recorder(tree, os.environ, llm=spec.llm_spec.dump())
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
    # summoner attribution, or a nested session would stamp it on its own
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
    env['CLAUDE_CODE_TMPDIR'] = str(_claude_temp_dir())
    _apply_mcp_backstops(env)
    # claude resolves fast-mode availability from a stored OAuth credentials
    # file, and left to guess without one reports it disabled by an organization
    env['CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK'] = '1'
    apply_shell_prefix(env, _claude_state_dir())
    apply_claude_auth(env, warn_when_missing=True)
    log.info('launching claude')
    if spec.solo:
      if launch.prompt is None:
        raise RuntimeError('a solo session launches with a prompt')
      if summoned():
        code = _run_claude_summoned(binary, launch.argv, env, launch.prompt)
      else:
        code = _run_claude_root_solo(binary, launch.argv, env, launch.prompt)
    elif summoned():
      code = _run_claude_summoned_interactive(binary, launch.argv, env, transcripts)
    else:
      code = _run_claude(binary, launch.argv, env, transcripts).code
    if code != 0:
      log.error('claude exited with status %d', code)

  return code
