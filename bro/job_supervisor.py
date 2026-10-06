"""Process-group leader that reaps every descendant of one shell command."""

import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import IO, Optional

_PR_SET_CHILD_SUBREAPER = 36
_OWNER_TERM_GRACE_SECONDS = 5.0


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


def _watch_owner(owner_fd: int) -> None:
  with os.fdopen(owner_fd, 'rb') as owner:
    while owner.read(1) != b'':
      pass
  with contextlib.suppress(ProcessLookupError):
    os.killpg(os.getpgrp(), signal.SIGTERM)
  deadline = time.monotonic() + _OWNER_TERM_GRACE_SECONDS
  while _group_has_other_processes() and time.monotonic() < deadline:
    time.sleep(0.05)
  if _group_has_other_processes():
    with contextlib.suppress(ProcessLookupError):
      os.killpg(os.getpgrp(), signal.SIGKILL)


def supervise(
  command: str,
  ready_fd: int,
  owner_fd: int,
  *,
  output: Optional[IO[bytes]] = None,
  finished: Optional[Callable[[int], None]] = None,
) -> int:
  """Run one shell until it and every descendant exit, or the owner handle closes."""
  subreaper = _become_subreaper()
  termination_signal: Optional[int] = None

  def terminate(signal_number: int, frame: Optional[FrameType]) -> None:
    nonlocal termination_signal
    del frame
    termination_signal = signal_number

  signal.signal(signal.SIGTERM, terminate)
  shell = subprocess.Popen(
    ['bash', '-c', command],
    stdout=output,
    stderr=subprocess.STDOUT if output is not None else None,
  )
  threading.Thread(target=_watch_owner, args=(owner_fd,), daemon=True).start()
  with os.fdopen(ready_fd, 'wb', buffering=0) as ready:
    with contextlib.suppress(BrokenPipeError):
      ready.write(b'1')
  shell_status = shell.wait()
  _wait_for_descendants(subreaper)
  exit_signal = termination_signal
  if exit_signal is None and shell_status < 0:
    exit_signal = -shell_status
  result = -exit_signal if exit_signal is not None else shell_status
  if finished is not None:
    finished(result)
  if exit_signal is not None:
    if exit_signal not in {signal.SIGKILL, signal.SIGSTOP}:
      signal.signal(exit_signal, signal.SIG_DFL)
    os.kill(os.getpid(), exit_signal)
    raise RuntimeError('termination signal did not end the supervisor')
  return shell_status


def _run() -> int:
  if len(sys.argv) == 4:
    return supervise(sys.argv[3], int(sys.argv[1]), int(sys.argv[2]))
  if len(sys.argv) == 3:
    owner_read_fd, owner_write_fd = os.pipe()
    try:
      return supervise(sys.argv[2], int(sys.argv[1]), owner_read_fd)
    finally:
      os.close(owner_write_fd)
  raise ValueError('usage: python -m bro.job_supervisor READY_FD [OWNER_FD] COMMAND')


if __name__ == '__main__':
  sys.exit(_run())
