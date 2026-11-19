"""the credential a live probe signs Claude Code in with, the terminal a probe
runs its TUI on, and the session state a ride runner would own around it.

A probe holds undocumented Claude Code behavior at the version managed sessions
run, so it drives that pinned release, the `claude` fixture, rather than whichever
`claude` the host has installed."""

import contextlib
import fcntl
import os
import pty
import shlex
import signal
import struct
import subprocess
import termios
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from bro import watches
from bro.base import credentials
from bro.base.suite_environment import host_credential_store
from bro.claude.waiter_state import WaiterState
from bro.monitor import SESSION_DIR_ENV


def claude_token() -> str | None:
  with host_credential_store():
    return credentials.try_get('claude_code')


REQUIRES_CLAUDE_CREDENTIAL = pytest.mark.skipif(
  claude_token() is None, reason='needs the claude_code credential'
)


@contextlib.contextmanager
def bounded(seconds: float) -> Iterator[None]:
  """fail the block after `seconds`, so a session that never ends fails the
  probe instead of hanging the stage; the run's own teardown ends claude."""

  def _expire(signum, frame):
    del signum, frame
    raise TimeoutError(f'the session did not end within {seconds:.0f}s')

  previous = signal.signal(signal.SIGALRM, _expire)
  signal.alarm(int(seconds))
  try:
    yield
  finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


@contextlib.contextmanager
def terminal(argv: list[str], env: dict[str, str], cwd: Path) -> Iterator[subprocess.Popen]:
  """run `argv` on a pty of its own, draining what it draws, until the block ends."""
  parent_end, child_end = pty.openpty()
  fcntl.ioctl(child_end, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 160, 0, 0))
  process = subprocess.Popen(
    argv,
    env={**env, 'TERM': 'xterm-256color'},
    cwd=cwd,
    stdin=child_end,
    stdout=child_end,
    stderr=child_end,
    start_new_session=True,
  )
  os.close(child_end)

  def _drain() -> None:
    with contextlib.suppress(OSError):
      while len(os.read(parent_end, 65536)) > 0:
        pass

  drain = threading.Thread(target=_drain, daemon=True)
  drain.start()
  try:
    yield process
  finally:
    process.terminate()
    try:
      process.wait(timeout=30)
    except subprocess.TimeoutExpired:
      process.kill()
      process.wait()
    os.close(parent_end)
    drain.join()


@contextlib.contextmanager
def watched_session(directory: Path) -> Iterator[tuple[watches.Store, WaiterState]]:
  """a session state dir this process owns as `do-ride` would: its watch store
  and waiter state, published in the environment the claude it runs and that
  claude's waiters inherit."""
  published = {SESSION_DIR_ENV: str(directory), 'RIDE_RUNNER_PID': str(os.getpid())}
  with mock.patch.dict(os.environ, published), watches.Owner.for_session() as owner:
    waiters = WaiterState.for_session()
    waiters.reset()
    try:
      yield owner.store, waiters
    finally:
      waiters.stand_down()


class Feed:
  """one live watch through which a probe hands the session its lines."""

  def __init__(self, store: watches.Store, path: Path) -> None:
    self._store = store
    self._path = path
    path.touch()
    self.command = shlex.join(['tail', '-n', '+1', '-f', str(path)])
    store.start(self.command)

  def say(self, line: str) -> None:
    with self._path.open('a') as lines:
      lines.write(f'{line}\n')

  def stop(self) -> None:
    self._store.stop(self.command)
