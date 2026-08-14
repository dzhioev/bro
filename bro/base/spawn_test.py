"""the guarantee under test: a child spawned via these wrappers is detached from any
controlling terminal, so an interactive prompt (read of /dev/tty) fails fast instead
of blocking the agent, and stdin defaults to /dev/null."""

import asyncio
import contextlib
import errno
import subprocess
from collections.abc import Iterator

import pytest

from bro.base import spawn
from bro.base.liveness_test_helper import Liveness
from bro.base.offload import off_loop


@pytest.fixture
def grandchild(tmp_path) -> Iterator[Liveness]:
  with contextlib.closing(Liveness(tmp_path / 'liveness')) as handle:
    yield handle


def _pipeline(grandchild: Liveness) -> str:
  """a shell pipeline whose left side takes the liveness handle with it, then blocks."""
  return f'({grandchild.holding("sleep 60")}) | cat'


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
    spawn.run(['bash', '-c', _pipeline(grandchild)], timeout=1, capture_output=True, text=True)
  grandchild.assert_reaped()


def test_run_check_raises_on_nonzero() -> None:
  with pytest.raises(subprocess.CalledProcessError):
    spawn.run(['false'], check=True)


def test_terminate_group_signals_the_whole_group(grandchild) -> None:
  # same shape as the timeout test: the pipeline's left side must receive the
  # SIGTERM too, not just the direct bash child.
  process = spawn.popen(['bash', '-c', _pipeline(grandchild)])
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
    await spawn.run_async(['bash', '-c', _pipeline(grandchild)], timeout=1)
  grandchild.assert_reaped()


@pytest.mark.asyncio
async def test_run_async_cancellation_kills_grandchildren(grandchild) -> None:
  # the interruption path: a cancelled tool call must not leave the shell it
  # started (or anything the shell started) running.
  task = asyncio.create_task(spawn.run_async(['bash', '-c', _pipeline(grandchild)], timeout=60))
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
