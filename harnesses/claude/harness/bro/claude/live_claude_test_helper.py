"""the credential a live probe signs Claude Code in with, the terminal a probe
runs its TUI on, and the session state a ride runner would own around it.

A probe holds undocumented Claude Code behavior at the version managed sessions
run, so it drives that pinned release, the `claude` fixture, rather than whichever
`claude` the host has installed."""

import contextlib
import fcntl
import os
import pty
import re
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


# a control sequence a terminal draws with: CSI, OSC, a character set
# designation, or a two-byte escape
_CONTROL_SEQUENCE = re.compile(
  rb'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[@-_]'
)


def _visible(drawn: bytes) -> str:
  """what `drawn` shows, without its control sequences and whitespace: a TUI
  moves its cursor over the gaps it redraws rather than printing spaces."""
  return re.sub(r'\s', '', _CONTROL_SEQUENCE.sub(b'', drawn).decode('utf-8', 'replace'))


class Terminal:
  """a process on a pty of its own: what it has drawn, and the lines typed at it."""

  def __init__(self, process: subprocess.Popen, parent_end: int) -> None:
    self.process = process
    self._parent_end = parent_end
    self._drawn = bytearray()
    self._grown = threading.Condition()

  def drain(self) -> None:
    with contextlib.suppress(OSError):
      while len(chunk := os.read(self._parent_end, 65536)) > 0:
        with self._grown:
          self._drawn.extend(chunk)
          self._grown.notify_all()

  def await_drawn(self, text: str, *, since: int = 0, timeout: float = 60) -> None:
    """block until the terminal has drawn `text` past byte `since` of its output."""
    expected = re.sub(r'\s', '', text)
    with self._grown:
      drawn = self._grown.wait_for(
        lambda: expected in _visible(bytes(self._drawn[since:])), timeout=timeout
      )
    assert drawn, f'the terminal did not draw {text!r} within {timeout:.0f}s'

  def type_line(self, text: str) -> None:
    """type `text`, and press enter once the terminal has drawn it."""
    with self._grown:
      mark = len(self._drawn)
    os.write(self._parent_end, text.encode())
    self.await_drawn(text, since=mark)
    os.write(self._parent_end, b'\r')


@contextlib.contextmanager
def terminal(argv: list[str], env: dict[str, str], cwd: Path) -> Iterator[Terminal]:
  """run `argv` on a pty of its own, keeping what it draws, until the block ends."""
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
  running = Terminal(process, parent_end)
  drain = threading.Thread(target=running.drain, daemon=True)
  drain.start()
  try:
    yield running
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
