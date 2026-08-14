"""the guarantee under test: a child spawned via these wrappers is detached from any
controlling terminal, so an interactive prompt (read of /dev/tty) fails fast instead
of blocking the agent, and stdin defaults to /dev/null."""

import asyncio
import contextlib
import errno
import os
import select
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

import pytest

from bro.base import spawn
from bro.base.offload import off_loop

_UP_MARKER = 'up'
_HANG_TIMEOUT = 10.0


class _Grandchild:
  """the left side of a shell pipeline, holding a FIFO open for as long as it lives.

  Liveness is read off that FIFO: the read end reaches EOF exactly when the last
  holder of a write end is gone — an identity a recycled pid cannot wear. This side
  holds a writer of its own until it asks, so a FIFO the grandchild has not opened
  yet never reads as one whose writer has died."""

  def __init__(self, fifo: Path) -> None:
    os.mkfifo(fifo)
    self._fifo = fifo
    self._read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # its writers open later
    self._write_fd: Optional[int] = os.open(fifo, os.O_WRONLY)
    self._started = False

  @property
  def command(self) -> str:
    """a shell pipeline whose left side takes the FIFO with it and then blocks."""
    return f'(exec 3>{self._fifo}; printf {_UP_MARKER} >&3; sleep 60) | cat'

  def wait_started(self) -> None:
    if self._started:
      return
    assert self._readable(), 'the grandchild never opened the FIFO'
    assert os.read(self._read_fd, len(_UP_MARKER)) == _UP_MARKER.encode()
    self._started = True

  def assert_reaped(self) -> None:
    self.wait_started()
    self._drop_writer()
    assert self._readable() and not os.read(self._read_fd, 1), 'the grandchild survived'

  def close(self) -> None:
    self._drop_writer()
    os.close(self._read_fd)

  def _drop_writer(self) -> None:
    if self._write_fd is not None:
      os.close(self._write_fd)
      self._write_fd = None

  def _readable(self) -> bool:
    # the timeout bounds a hang rather than a race: each wait ends on the event itself
    # — the grandchild's write, or the last write end closing — so a run that passes
    # spends none of it.
    ready, _, _ = select.select([self._read_fd], [], [], _HANG_TIMEOUT)
    return bool(ready)


@pytest.fixture
def grandchild(tmp_path) -> Iterator[_Grandchild]:
  with contextlib.closing(_Grandchild(tmp_path / 'liveness')) as handle:
    yield handle


def test_child_is_detached_into_own_session() -> None:
  # start_new_session makes the child a session leader (getsid == its own pid),
  # which means it inherits no controlling terminal from the parent.
  process = spawn.run(
    ['python3', '-c', 'import os; print(os.getpid(), os.getsid(0))'],
    capture_output=True,
    text=True,
    timeout=10,
  )
  pid, sid = process.stdout.split()
  assert pid == sid


def test_no_controlling_tty_so_dev_tty_open_fails() -> None:
  # with no controlling terminal, opening /dev/tty raises ENXIO immediately rather
  # than blocking — the exact failure mode that turns a credential prompt into an
  # error instead of a hang.
  process = spawn.run(
    ['python3', '-c', 'open("/dev/tty")'],
    capture_output=True,
    text=True,
    timeout=10,
  )
  assert process.returncode != 0
  # ENXIO surfaces with a platform-specific strerror ("No such device or address" on
  # Linux, "Device not configured" on macOS), so match the errno rather than the text.
  assert f'Errno {errno.ENXIO}' in process.stderr


def test_run_redirects_stdin_to_devnull_by_default() -> None:
  process = spawn.run(['cat'], capture_output=True, text=True, timeout=10)
  assert process.returncode == 0
  assert process.stdout == ''


def test_run_caller_can_override_stdin_with_input() -> None:
  process = spawn.run(['cat'], input='hello', capture_output=True, text=True, timeout=10)
  assert process.stdout == 'hello'


def test_run_returns_completed_process() -> None:
  process = spawn.run(['true'])
  assert isinstance(process, subprocess.CompletedProcess)
  assert process.returncode == 0


def test_run_timeout_kills_grandchildren(grandchild) -> None:
  # the bug this guards against: a timed-out shell pipeline must take its whole
  # process group with it. If only the shell were killed, the blocked left side of
  # the pipe would survive as an orphan.
  with pytest.raises(subprocess.TimeoutExpired):
    spawn.run(['bash', '-c', grandchild.command], timeout=1, capture_output=True, text=True)
  grandchild.assert_reaped()


def test_run_check_raises_on_nonzero() -> None:
  with pytest.raises(subprocess.CalledProcessError):
    spawn.run(['false'], check=True)


def test_terminate_group_signals_the_whole_group(grandchild) -> None:
  # same shape as the timeout test: the pipeline's left side must receive the
  # SIGTERM too, not just the direct bash child.
  process = spawn.popen(['bash', '-c', grandchild.command])
  grandchild.wait_started()
  spawn.terminate_group(process)
  assert process.wait(timeout=10) == -15
  grandchild.assert_reaped()


@pytest.mark.asyncio
async def test_run_async_captures_output_and_exit_code() -> None:
  process = await spawn.run_async(['bash', '-c', 'echo out; echo err 1>&2; exit 3'])
  assert process.returncode == 3
  assert process.stdout == 'out\n'
  assert process.stderr == 'err\n'


@pytest.mark.asyncio
async def test_run_async_timeout_kills_grandchildren(grandchild) -> None:
  with pytest.raises(subprocess.TimeoutExpired):
    await spawn.run_async(['bash', '-c', grandchild.command], timeout=1)
  grandchild.assert_reaped()


@pytest.mark.asyncio
async def test_run_async_cancellation_kills_grandchildren(grandchild) -> None:
  # the interruption path: a cancelled tool call must not leave the shell it
  # started (or anything the shell started) running.
  task = asyncio.create_task(spawn.run_async(['bash', '-c', grandchild.command], timeout=60))
  await off_loop(grandchild.wait_started)
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  grandchild.assert_reaped()


def test_popen_streams_and_detaches() -> None:
  process = spawn.popen(
    ['python3', '-c', 'import os; print(os.getsid(0) == os.getpid())'],
    stdout=subprocess.PIPE,
    text=True,
  )
  out, _ = process.communicate(timeout=10)
  assert out.strip() == 'True'
