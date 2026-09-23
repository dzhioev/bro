"""The Claude harness runner under `do-ride`.

It assumes its cwd is a prepared workspace tree under the session runtime environment.
It owns everything that runs next to Claude:
resume resolution, argv, the session-local MCP server, launch declarations, and the recorder daemon.
The outer `ride solo|along` validates policy once, so this runner repeats no policy gates.
"""

import contextlib
import os
import threading
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from bro.base import log
from bro.monitor import SESSION_DIR_ENV, claude_projects_dir, harness_session_dir, trail_pointer
from bro.run_lifecycle import RunLifecycle
from bro.summon import SUMMONER_ENV, summoned
from ride.claude.claude_argv import build_claude_launch
from ride.claude.claude_auth import apply_claude_auth
from ride.claude.claude_config import latest_jsonl
from ride.claude.interrupt import Run, run_interactive, run_streaming
from ride.claude.mcp import start_session_mcp_server
from ride.claude.recorder import start_session_recorder
from ride.claude.shell_prefix import apply_shell_prefix
from ride.claude.statusline import start_statusline_projector

if TYPE_CHECKING:
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


def _run_claude(argv: list[str], env: dict[str, str], transcripts: Path) -> Run:
  return run_interactive(['claude', *argv], env, transcripts)


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


def _run_claude_root_solo(argv: list[str], env: dict[str, str], prompt: str) -> int:
  """run a root's print-mode Claude with each turn's reply on the session's
  stdout, and close its host-anchored quest on success."""
  with _trail_watch() as emitted:
    run = run_streaming(
      ['claude', *argv], env, prompt, on_result=lambda reply: print(reply, flush=True)
    )
  if run.code == 0 and not run.stopped:
    _complete_run(emitted, None)
  return run.code


def _run_claude_summoned(argv: list[str], env: dict[str, str], prompt: str) -> int:
  """run a summoned print-mode Claude and emit its broker lifecycle.

  A clean exit answers with the last turn's reply. A non-zero exit or a stopped
  run emits no result: the broker synthesizes `result{failed}` from reap for the
  former, and a `raise`- or `answer`-ended session already sent its own. The
  reply is echoed to stdout either way, so the output tail still carries it."""
  with _trail_watch() as emitted:
    run = run_streaming(['claude', *argv], env, prompt)
  reply = run.results[-1] if len(run.results) > 0 else ''
  print(reply, flush=True)
  if run.code != 0 or run.stopped:
    return run.code
  _complete_run(emitted, reply)
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


def run_session(spec: 'SessionSpec | SessionRun') -> int:
  tree = Path.cwd()

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
    # session-local MCP serving: OS-assigned port published via a port file,
    # per-session bearer token. the server imports from the session runtime
    # selected by PATH — the snapshot on host, the runtime volume in a container.
    try:
      server = start_session_mcp_server(f'persona:{spec.bro}', tree, os.environ)
    except RuntimeError as error:
      log.error('%s', error)
      return 1
    teardown.callback(server.stop)

    launch = build_claude_launch(spec, claude_args=claude_args, endpoint=server.endpoint)
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
    # claude's MCP tool-call timeout (ms): the ~1-minute default kills
    # legitimately slow tools (vision audits, renders)
    env['MCP_TOOL_TIMEOUT'] = str(10 * 60 * 1000)
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
        code = _run_claude_summoned(launch.argv, env, launch.prompt)
      else:
        code = _run_claude_root_solo(launch.argv, env, launch.prompt)
    elif summoned():
      code = _run_claude_summoned_interactive(launch.argv, env, transcripts)
    else:
      code = _run_claude(launch.argv, env, transcripts).code
    if code != 0:
      log.error('claude exited with status %d', code)

  return code
