"""ending a claude process so its in-flight turn reaches the transcript.

Claude Code persists an assistant message only once every tool call it carries
resolves, and the terminating service tools (`answer`, `raise`) deliberately
never resolve their own — so the turn holding one is written only when claude is
interrupted: it then writes the assistant `tool_use` blocks with a rejected
`tool_result` for each unresolved call, and stops working. A TUI killed instead
takes the whole turn down with it — the terminal payload and the sibling calls
batched beside it.

Reaching that interrupt from outside the session takes a different mechanism per
flavor. Print-mode claude takes SIGINT as the interrupt and exits on it. A TUI
claude reads its terminal in raw mode, where Ctrl-C is a keypress rather than a
signal, and treats SIGINT as quit-now instead — so its interrupt has to arrive
as that keypress, which is why an interactive run gives claude a pty of its own
and proxies the session's terminal through it.
"""

import contextlib
import fcntl
import os
import pty
import selectors
import signal
import subprocess
import termios
import threading
import time
import tty
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

from bro.base import log
from ride.claude.claude_config import latest_jsonl
from ride.inner import stopped_on_sigterm

# the bytes a terminal delivers for Ctrl-C and Ctrl-D
_INTERRUPT_KEY = b'\x03'
_EOF_KEY = b'\x04'

# the session's own terminal, addressed as descriptors: the proxy moves bytes
# rather than going through python's buffered wrappers
_SESSION_STDIN = 0
_SESSION_STDOUT = 1

_PROXY_CHUNK = 65536
# the proxy's wake-up cadence, which bounds how long its shutdown waits
_PROXY_POLL_SECONDS = 0.2

_POLL_SECONDS = 0.05
# claude's own latency between taking the interrupt and its first write
_FLUSH_GRACE_SECONDS = 0.5
# a transcript untouched for this long has the interrupted turn fully written
_FLUSH_SETTLE_SECONDS = 0.5
_FLUSH_TIMEOUT_SECONDS = 15.0
# how long claude is given to go on its own before the stop turns into a kill
_EXIT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class PrintedRun:
  """a finished print-mode claude run: its exit code, the reply it printed, and
  whether a stop request rather than the agent's own completion ended it."""

  code: int
  output: str
  stopped: bool


def run_printing(argv: list[str], env: Mapping[str, str]) -> PrintedRun:
  """run print-mode claude to completion, capturing its printed reply."""
  process = subprocess.Popen(argv, env=dict(env), stdout=subprocess.PIPE, text=True)
  with stopped_on_sigterm(lambda: _interrupt_printing(process)) as stopped:
    output, _ = process.communicate()
  return PrintedRun(process.returncode, output, stopped.is_set())


def run_interactive(argv: list[str], env: Mapping[str, str], transcripts: Path) -> int:
  """run claude's TUI on a pty proxying the session's terminal and return its
  exit code. `transcripts` is the projects dir the interrupted turn lands in."""
  with _terminal_run(argv, env) as run:
    with stopped_on_sigterm(lambda: _interrupt_interactive(run, transcripts)):
      return run.process.wait()


def _interrupt_printing(process: subprocess.Popen) -> None:
  process.send_signal(signal.SIGINT)
  _await_exit(process)


def _interrupt_interactive(run: '_TerminalRun', transcripts: Path) -> None:
  run.type(_INTERRUPT_KEY)
  try:
    _await_flush(transcripts)
  finally:
    # the TUI's own quit, taken once the flushed turn is on disk
    run.process.send_signal(signal.SIGINT)
    _await_exit(run.process)


def _await_flush(transcripts: Path) -> None:
  """wait for the interrupted turn to reach the session transcript."""
  time.sleep(_FLUSH_GRACE_SECONDS)
  deadline = time.monotonic() + _FLUSH_TIMEOUT_SECONDS
  while time.monotonic() < deadline:
    transcript = latest_jsonl(transcripts)
    if transcript is None or time.time() - transcript.stat().st_mtime >= _FLUSH_SETTLE_SECONDS:
      return
    time.sleep(_POLL_SECONDS)
  log.warning('the interrupted turn is still being written after %.0fs', _FLUSH_TIMEOUT_SECONDS)


def _await_exit(process: subprocess.Popen) -> None:
  """give claude the grace to go on its own, then take it down."""
  deadline = time.monotonic() + _EXIT_TIMEOUT_SECONDS
  while time.monotonic() < deadline:
    if process.poll() is not None:
      return
    time.sleep(_POLL_SECONDS)
  log.warning('claude did not exit %.0fs after the interrupt; terminating', _EXIT_TIMEOUT_SECONDS)
  process.terminate()


class _TerminalRun:
  """claude running on a pty that stands in for the session's own terminal."""

  def __init__(self, process: subprocess.Popen, terminal: int) -> None:
    self.process = process
    self._terminal = terminal

  def type(self, keys: bytes) -> None:
    """deliver `keys` to claude the way its terminal would."""
    _write_all(self._terminal, keys)


@contextlib.contextmanager
def _terminal_run(argv: list[str], env: Mapping[str, str]) -> Generator[_TerminalRun]:
  """run `argv` on a pty proxying this process's terminal for the block's
  duration."""
  terminal, child_end = pty.openpty()
  with contextlib.ExitStack() as teardown:
    teardown.callback(os.close, terminal)
    if os.isatty(_SESSION_STDIN):
      teardown.enter_context(_raw_mode(_SESSION_STDIN))
      teardown.enter_context(_window_size_followed(_SESSION_STDIN, terminal))
    process = subprocess.Popen(
      argv, env=dict(env), stdin=child_end, stdout=child_end, stderr=child_end
    )
    # the child holds the only handle on its end now, so the proxy reads EOF
    # from the pty when it exits
    os.close(child_end)
    teardown.enter_context(_proxied(terminal))
    yield _TerminalRun(process, terminal)


@contextlib.contextmanager
def _raw_mode(terminal: int) -> Generator[None]:
  """put the terminal in raw mode for the block's duration, so every keypress —
  the interrupt among them — reaches the proxied pty as a byte."""
  saved = termios.tcgetattr(terminal)
  tty.setraw(terminal)
  try:
    yield
  finally:
    termios.tcsetattr(terminal, termios.TCSADRAIN, saved)


@contextlib.contextmanager
def _window_size_followed(source: int, target: int) -> Generator[None]:
  """size `target` like `source` for the block's duration, resizes included."""

  def _follow(signum, frame):
    del signum, frame
    _copy_window_size(source, target)

  _copy_window_size(source, target)
  previous = signal.signal(signal.SIGWINCH, _follow)
  try:
    yield
  finally:
    signal.signal(signal.SIGWINCH, previous)


def _copy_window_size(source: int, target: int) -> None:
  size = fcntl.ioctl(source, termios.TIOCGWINSZ, bytes(8))
  fcntl.ioctl(target, termios.TIOCSWINSZ, size)


@contextlib.contextmanager
def _proxied(terminal: int) -> Generator[None]:
  """copy this process's terminal into the pty and back for the block's duration."""
  stop = threading.Event()
  thread = threading.Thread(target=_proxy, args=(terminal, stop), daemon=True)
  thread.start()
  try:
    yield
  finally:
    stop.set()
    thread.join()


def _proxy(terminal: int, stop: threading.Event) -> None:
  # select over epoll: epoll refuses a regular file, which a session's stdin can
  # be, while select reports one always-ready — the right answer for it
  with selectors.SelectSelector() as selector:
    selector.register(terminal, selectors.EVENT_READ)
    selector.register(_SESSION_STDIN, selectors.EVENT_READ)
    while not stop.is_set():
      for ready, _ in selector.select(timeout=_PROXY_POLL_SECONDS):
        if ready.fd == terminal:
          try:
            output = os.read(terminal, _PROXY_CHUNK)
          except OSError:
            return  # EIO: the child's end of the pty is gone, so the session is over
          if len(output) == 0:
            return
          _write_all(_SESSION_STDOUT, output)
          continue
        keys = os.read(_SESSION_STDIN, _PROXY_CHUNK)
        if len(keys) == 0:
          # a pty carries no end of its own, so a closed session terminal
          # reaches claude as the key its terminal would have sent
          selector.unregister(_SESSION_STDIN)
          keys = _EOF_KEY
        _write_all(terminal, keys)


def _write_all(fd: int, data: bytes) -> None:
  while len(data) > 0:
    data = data[os.write(fd, data) :]
