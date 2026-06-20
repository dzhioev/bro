"""the guarantee under test: a child spawned via these wrappers is detached from any
controlling terminal, so an interactive prompt (read of /dev/tty) fails fast instead
of blocking the agent, and stdin defaults to /dev/null."""

import subprocess

from base import spawn


def test_child_is_detached_into_own_session() -> None:
  # start_new_session makes the child a session leader (getsid == its own pid),
  # which means it inherits no controlling terminal from the parent.
  proc = spawn.run(
    ['python3', '-c', 'import os; print(os.getpid(), os.getsid(0))'],
    capture_output=True,
    text=True,
    timeout=10,
  )
  pid, sid = proc.stdout.split()
  assert pid == sid


def test_no_controlling_tty_so_dev_tty_open_fails() -> None:
  # with no controlling terminal, opening /dev/tty raises ENXIO immediately rather
  # than blocking — the exact failure mode that turns a credential prompt into an
  # error instead of a hang.
  proc = spawn.run(
    ['python3', '-c', 'open("/dev/tty")'],
    capture_output=True,
    text=True,
    timeout=10,
  )
  assert proc.returncode != 0
  assert 'No such device or address' in proc.stderr


def test_run_redirects_stdin_to_devnull_by_default() -> None:
  proc = spawn.run(['cat'], capture_output=True, text=True, timeout=10)
  assert proc.returncode == 0
  assert proc.stdout == ''


def test_run_caller_can_override_stdin_with_input() -> None:
  proc = spawn.run(['cat'], input='hello', capture_output=True, text=True, timeout=10)
  assert proc.stdout == 'hello'


def test_run_returns_completed_process() -> None:
  proc = spawn.run(['true'])
  assert isinstance(proc, subprocess.CompletedProcess)
  assert proc.returncode == 0


def test_popen_streams_and_detaches() -> None:
  proc = spawn.popen(
    ['python3', '-c', 'import os; print(os.getsid(0) == os.getpid())'],
    stdout=subprocess.PIPE,
    text=True,
  )
  out, _ = proc.communicate(timeout=10)
  assert out.strip() == 'True'
