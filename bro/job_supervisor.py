"""Process-group leader that reaps every descendant of one shell command."""

import ctypes
import os
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import Optional

_PR_SET_CHILD_SUBREAPER = 36


def _become_subreaper() -> bool:
  if sys.platform != 'linux':
    return False
  library = ctypes.CDLL(None, use_errno=True)
  if library.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number))
  return True


def _group_has_other_processes() -> bool:
  process = subprocess.run(
    ['ps', '-eo', 'pid=,pgid='],
    check=True,
    capture_output=True,
    text=True,
    start_new_session=True,
  )
  own_process_id = os.getpid()
  own_group_id = os.getpgrp()
  return any(
    int(process_id) != own_process_id and int(group_id) == own_group_id
    for line in process.stdout.splitlines()
    for process_id, group_id in [line.split()]
  )


def _wait_for_descendants(subreaper: bool) -> None:
  if subreaper:
    while True:
      try:
        os.wait()
      except ChildProcessError:
        return
  while _group_has_other_processes():
    time.sleep(0.05)


def supervise(command: str, ready_fd: int) -> int:
  subreaper = _become_subreaper()
  termination_signal: Optional[int] = None

  def terminate(signal_number: int, frame: Optional[FrameType]) -> None:
    nonlocal termination_signal
    del frame
    termination_signal = signal_number

  signal.signal(signal.SIGTERM, terminate)
  shell = subprocess.Popen(['bash', '-c', command])
  with os.fdopen(ready_fd, 'wb') as ready:
    ready.write(b'1')
  shell_status = shell.wait()
  _wait_for_descendants(subreaper)
  exit_signal = termination_signal
  if exit_signal is None and shell_status < 0:
    exit_signal = -shell_status
  if exit_signal is not None:
    if exit_signal not in {signal.SIGKILL, signal.SIGSTOP}:
      signal.signal(exit_signal, signal.SIG_DFL)
    os.kill(os.getpid(), exit_signal)
    raise RuntimeError('termination signal did not end the supervisor')
  return shell_status


def _run() -> int:
  if len(sys.argv) != 3:
    raise ValueError('usage: python -m bro.job_supervisor READY_FD COMMAND')
  return supervise(sys.argv[2], int(sys.argv[1]))


if __name__ == '__main__':
  sys.exit(_run())
