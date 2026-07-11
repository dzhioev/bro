"""the guarantee under test: a child spawned via these wrappers is detached from any
controlling terminal, so an interactive prompt (read of /dev/tty) fails fast instead
of blocking the agent, and stdin defaults to /dev/null."""

import errno
import os
import subprocess
import time

import pytest

from base import spawn


def _running(pid: int) -> bool:
  """True only if pid names a live, *running* process. A killed grandchild that gets
  orphaned lingers as a zombie wherever pid 1 doesn't reap it; `os.kill(pid, 0)`
  succeeds on a zombie, so it can't tell a dead-but-unreaped process from a live one.
  Read the process state directly to exclude zombies, so this asserts what the test
  means (not running) without depending on the ambient reaper. Where /proc is absent
  (e.g. a macOS host) existence implies running."""
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  try:
    stat = open(f'/proc/{pid}/stat').read()
  except FileNotFoundError:
    return True
  except ProcessLookupError:  # reaped between the kill(0) probe and this read (ESRCH)
    return False
  return stat.rsplit(')', 1)[1].split()[0] != 'Z'


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


def test_run_timeout_kills_grandchildren(tmp_path) -> None:
  # the bug this guards against: a timed-out shell pipeline must take its whole
  # process group with it. The left side of the pipe records its pid and then
  # blocks; if only the shell were killed it would survive as an orphan.
  pidfile = tmp_path / 'pid'
  command = f'(echo $BASHPID > {pidfile}; sleep 60) | cat'
  with pytest.raises(subprocess.TimeoutExpired):
    spawn.run(['bash', '-c', command], timeout=1, capture_output=True, text=True)
  pid = int(pidfile.read_text())
  deadline = time.time() + 5
  while time.time() < deadline and _running(pid):
    time.sleep(0.05)
  assert not _running(pid), 'grandchild survived the timeout'


def test_run_check_raises_on_nonzero() -> None:
  with pytest.raises(subprocess.CalledProcessError):
    spawn.run(['false'], check=True)


def test_terminate_group_signals_the_whole_group(tmp_path) -> None:
  # same shape as the timeout test: the pipeline's left side must receive the
  # SIGTERM too, not just the direct bash child.
  pidfile = tmp_path / 'pid'
  process = spawn.popen(['bash', '-c', f'(echo $BASHPID > {pidfile}; sleep 60) | cat'])
  deadline = time.time() + 5
  while time.time() < deadline and (not pidfile.exists() or len(pidfile.read_text()) == 0):
    time.sleep(0.05)
  pid = int(pidfile.read_text())
  spawn.terminate_group(process)
  assert process.wait(timeout=10) == -15
  deadline = time.time() + 5
  while time.time() < deadline and _running(pid):
    time.sleep(0.05)
  assert not _running(pid), 'grandchild survived terminate_group'


def test_popen_streams_and_detaches() -> None:
  process = spawn.popen(
    ['python3', '-c', 'import os; print(os.getsid(0) == os.getpid())'],
    stdout=subprocess.PIPE,
    text=True,
  )
  out, _ = process.communicate(timeout=10)
  assert out.strip() == 'True'
