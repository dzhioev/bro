"""the inner half of a managed session: the command the outer launch spawns in
the prepared workspace, and what ride does there before the harness's own
session starts.

The outer layer (`ride/session.py`) prepares the workspace and spawns this
module's `--in-place` entry inside it — a host worktree runner or the container's
main process. Everything a session carries regardless of the agent loop driving
it lives here; the harness supplies only its own runner.
"""

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.launch.broxy import session_broxy
from bro.registry import create_bro
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
def sigterm_forwarded_to(process: subprocess.Popen) -> Generator[threading.Event]:
  """forward SIGTERM to `process` for the block's duration, yielding the event
  that records a forward happened."""
  forwarded = threading.Event()

  def _forward(signum, frame):
    del signum, frame
    forwarded.set()
    process.terminate()

  previous = signal.signal(signal.SIGTERM, _forward)
  try:
    yield forwarded
  finally:
    signal.signal(signal.SIGTERM, previous)


def run_agent(argv: list[str], env: Optional[dict[str, str]] = None) -> int:
  """spawn the session's agent process and wait, forwarding SIGTERM to it — a
  SIGTERM aimed at the runner (`docker stop`, kill, terminate_session) would
  otherwise strand it. the runner keeps waiting after forwarding, so the
  post-exit work still runs."""
  process = subprocess.Popen(argv, env=env)
  with sigterm_forwarded_to(process):
    return process.wait()


def run_in_place(harness: 'Harness', spec: 'SessionSpec') -> int:
  """run the session in the workspace this process was spawned in."""
  os.environ.update(bro_git_identity_env(spec.bro))
  # RIDE_BRO themes the session (banner, statusLine)
  os.environ['RIDE_BRO'] = spec.bro
  create_bro(spec.bro).provision_workspace(Path.cwd())
  # a host launch signals the session broxy through BRO_START_SESSION_BROXY (in
  # a container the entrypoint started one and BROKER_CHANNEL already points at
  # it), rewriting BROKER_CHANNEL before anything the session spawns inherits
  # the environment. a set BROKER_CHANNEL always names a broxy socket: when the
  # broxy cannot run the channel is unset — the session runs without one — and
  # the launch proceeds.
  with session_broxy():
    return harness.run_in_place(spec)
