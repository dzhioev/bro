"""the inner half of a managed session: the command the outer launch spawns in
the prepared workspace, and what ride does there before the harness's own
session starts.

The outer layer (`ride/session.py`) prepares the workspace and spawns this
module's `--in-place` entry with that workspace as cwd — from the runtime snapshot
on the host or the runtime volume in a container. Everything a session carries
regardless of the agent loop driving it lives here; the harness supplies only its
own runner.
"""

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.launch.broxy import session_broxy
from bro.launch.hold import HOLD_VARIABLE
from bro.registry import create_bro
from bro.workspace.session import clear_requested_exit_status, requested_exit_status
from ride.identity import bro_git_identity_env

if TYPE_CHECKING:
  from ride.harness import Harness
  from ride.session import SessionSpec


def inner_command(spec: 'SessionSpec', *, harness_flags: Sequence[str]) -> list[str]:
  """the argv that re-enters ride in the workspace, carrying the session spec
  across the process boundary."""
  verb = 'solo' if spec.solo else 'along'
  flags = {'--resume': spec.resume, '--no-trails': spec.no_trails}
  parts = [
    'ride',
    verb,
    '--in-place',
    '--workspace',
    spec.name,
    '--harness',
    spec.harness,
    *(flag for flag, enabled in flags.items() if enabled),
    *harness_flags,
  ]
  if spec.repo is not None:
    parts.extend(['--repo', spec.repo])
  parts.extend(['--hold', spec.hold])
  if spec.llm is not None:
    parts.extend(['--llm', spec.llm])
  parts.append(spec.bro)
  if spec.prompt is not None:
    parts.append(spec.prompt)
  if len(spec.arguments) > 0:
    parts.extend(['--', *spec.arguments])
  return parts


@contextlib.contextmanager
def stopped_on_sigterm(stop: Callable[[], None]) -> Generator[threading.Event]:
  """run `stop` when SIGTERM arrives, for the block's duration, yielding the
  event that records one did. `stop` runs on a thread of its own — ending a
  harness process gracefully takes seconds, which a signal handler must not
  spend — and a repeat signal is ignored rather than starting a second."""
  stopped = threading.Event()

  def _stop(signum, frame):
    del signum, frame
    if stopped.is_set():
      return
    stopped.set()
    threading.Thread(target=stop, daemon=True).start()

  previous = signal.signal(signal.SIGTERM, _stop)
  try:
    yield stopped
  finally:
    signal.signal(signal.SIGTERM, previous)


def run_agent(argv: list[str], env: Optional[dict[str, str]] = None) -> int:
  """spawn the session's agent process and wait, terminating it on SIGTERM — a
  SIGTERM aimed at the runner (`docker stop`, kill, terminate_session) would
  otherwise strand it. the runner keeps waiting after the stop, so the
  post-exit work still runs."""
  process = subprocess.Popen(argv, env=env)
  with stopped_on_sigterm(process.terminate):
    return process.wait()


def run_in_place(harness: 'Harness', spec: 'SessionSpec') -> int:
  """run the session in the workspace this process was spawned in."""
  os.environ.update(bro_git_identity_env(spec.bro))
  # RIDE_BRO themes the session (banner, statusLine)
  os.environ['RIDE_BRO'] = spec.bro
  # the hold and this runner's pid overwrite any ambient value: a session
  # launched from inside another must inherit neither its hold nor a kill target
  # naming a foreign runner
  os.environ[HOLD_VARIABLE] = spec.hold
  os.environ['RIDE_RUNNER_PID'] = str(os.getpid())
  if spec.repo is not None:
    create_bro(spec.bro).provision_workspace(Path.cwd())
  clear_requested_exit_status()
  # A host launch signals the session broxy through BRO_START_SESSION_BROXY.
  # Container entrypoints have already consumed BROKER_UPSTREAM and published
  # BROKER_CHANNEL before this process starts.
  with session_broxy():
    code = harness.run_in_place(spec)
  requested = requested_exit_status()
  return code if requested is None else requested
