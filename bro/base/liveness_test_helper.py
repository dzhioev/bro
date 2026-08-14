"""liveness of a spawned process, reported by an fd rather than by its pid.

A pid names a process only until the kernel recycles the number, and death is not
an event a test can await from outside the process's parent. Both go away when the
process under test holds one end of a FIFO: the other end reaches EOF exactly when
the last holder is gone, so the test blocks on the event itself instead of polling
a deadline."""

import os
import select
from pathlib import Path
from typing import Optional

_HANDLE_FD = 3
_UP_MARKER = 'up'
_HANG_TIMEOUT = 10.0


class Liveness:
  """a FIFO whose write end a spawned process holds for as long as it lives.

  This side holds a writer of its own until it asks, so a FIFO the process has not
  opened yet never reads as one whose writer has died."""

  def __init__(self, fifo: Path) -> None:
    os.mkfifo(fifo)
    self._fifo = fifo
    self._read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # its writers open later
    self._write_fd: Optional[int] = os.open(fifo, os.O_WRONLY)
    self._started = False

  def holding(self, command: str) -> str:
    """a shell command that takes the FIFO and reports itself up, then runs `command`.
    Whatever `command` execs inherits the fd, so it keeps reporting for that process."""
    return f'exec {_HANDLE_FD}>{self._fifo}; printf {_UP_MARKER} >&{_HANDLE_FD}; {command}'

  def wait_started(self) -> None:
    if self._started:
      return
    assert self._readable(), 'the process never opened the FIFO'
    assert os.read(self._read_fd, len(_UP_MARKER)) == _UP_MARKER.encode()
    self._started = True

  def assert_reaped(self) -> None:
    self.wait_started()
    self._drop_writer()
    assert self._readable() and not os.read(self._read_fd, 1), 'the process survived'

  def close(self) -> None:
    self._drop_writer()
    os.close(self._read_fd)

  def _drop_writer(self) -> None:
    if self._write_fd is not None:
      os.close(self._write_fd)
      self._write_fd = None

  def _readable(self) -> bool:
    # the timeout bounds a hang rather than a race: each wait ends on the event itself
    # — the process's write, or the last write end closing — so a run that passes
    # spends none of it.
    ready, _, _ = select.select([self._read_fd], [], [], _HANG_TIMEOUT)
    return bool(ready)
