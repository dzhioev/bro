"""Supervisor that runs one command in a process group of its own and outlives
every descendant the command leaves."""

import collections
import contextlib
import ctypes
import os
import select
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from types import FrameType
from typing import IO, NamedTuple, Optional

_PR_SET_CHILD_SUBREAPER = 36
_TERM_GRACE_SECONDS = 5.0
# without a subreaper, the exits of orphaned descendants send this process no
# SIGCHLD, so their group is polled instead
_GROUP_POLL_SECONDS = 0.05

KILL_SIGNAL = signal.SIGUSR1
"""The signal asking a supervisor to SIGKILL what it supervises; SIGTERM asks it
to terminate them."""

_ended = threading.Event()


def _become_subreaper() -> bool:
  if sys.platform != 'linux':
    return False
  library = ctypes.CDLL(None, use_errno=True)
  if library.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number))
  return True


class _Descendant(NamedTuple):
  process_id: int
  group_id: int
  name: bytes


def _descendants() -> list[_Descendant]:
  """Each live process descending from this one."""
  children: dict[int, list[_Descendant]] = collections.defaultdict(list)
  for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
      continue
    try:
      # a process name is any bytes
      stat = (entry / 'stat').read_bytes()
    except (FileNotFoundError, ProcessLookupError):
      continue
    head, tail = stat.rsplit(b')', 1)
    state, parent_id, group_id = tail.split()[:3]
    if state != b'Z':
      name = head.split(b'(', 1)[1]
      children[int(parent_id)].append(_Descendant(int(entry.name), int(group_id), name))
  found: list[_Descendant] = []
  pending = [os.getpid()]
  while len(pending) > 0:
    for child in children[pending.pop()]:
      found.append(child)
      pending.append(child.process_id)
  return found


def _refused(send: Callable[[int, int], None], target: int, signal_number: int) -> bool:
  """Whether `send(target, signal_number)` met a process this one may not signal."""
  try:
    send(target, signal_number)
  except ProcessLookupError:
    return False
  except PermissionError:
    return True
  return False


def _printable(name: bytes) -> str:
  """`name` as text, with invalid UTF-8 and unprintable characters escaped."""
  return ''.join(
    character if character.isprintable() else character.encode('unicode_escape').decode()
    for character in name.decode(errors='backslashreplace')
  )


def _drain(descriptor: int) -> None:
  with contextlib.suppress(BlockingIOError):
    while len(os.read(descriptor, 4096)) > 0:
      pass


@contextlib.contextmanager
def _signal_wakeup() -> Generator[int]:
  """A descriptor that turns readable whenever a signal with a Python handler
  arrives, whichever thread the kernel delivers it to."""
  read_fd, write_fd = os.pipe()
  os.set_blocking(read_fd, False)
  os.set_blocking(write_fd, False)
  with os.fdopen(read_fd, 'rb', buffering=0), os.fdopen(write_fd, 'wb', buffering=0):
    previous = signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
    try:
      yield read_fd
    finally:
      signal.set_wakeup_fd(previous)


class _Supervision:
  """One command, the descendants it leaves, and the end its owner asks for."""

  def __init__(self, argv: Sequence[str], output: Optional[IO[bytes]], subreaper: bool) -> None:
    self._subreaper = subreaper
    self._report_stream = output if output is not None else sys.stdout.buffer
    self._reported: set[str] = set()
    self._terminated = False
    self.requested: Optional[int] = None
    signal.signal(signal.SIGTERM, self._request)
    signal.signal(KILL_SIGNAL, self._request)
    # a handler of its own makes SIGCHLD reach the wakeup descriptor
    signal.signal(signal.SIGCHLD, lambda signal_number, frame: None)
    self._process = subprocess.Popen(
      argv,
      stdout=output,
      stderr=subprocess.STDOUT if output is not None else None,
      process_group=0,
    )

  def _request(self, signal_number: int, frame: Optional[FrameType]) -> None:
    del frame
    if signal_number == KILL_SIGNAL:
      self.requested = signal.SIGKILL
    elif self.requested is None:
      self.requested = signal.SIGTERM

  def wait(self) -> int:
    """Wait until the command and every descendant are gone; return the command's
    exit code."""
    with _signal_wakeup() as wakeup:
      while True:
        _drain(wakeup)
        status = self._process.poll()
        if status is not None and self._gone():
          return status
        # a kill sweeps on every wake, since descendants appear after a sweep:
        # forked while it listed them, or by a process it may not kill
        if self.requested == signal.SIGKILL:
          self._signal(signal.SIGKILL)
        elif self.requested == signal.SIGTERM and not self._terminated:
          self._signal(signal.SIGTERM)
          self._terminated = True
        select.select([wakeup], [], [], None if self._subreaper else _GROUP_POLL_SECONDS)

  def _gone(self) -> bool:
    """Reap every exited descendant; whether none is left."""
    if not self._subreaper:
      try:
        os.killpg(self._process.pid, 0)
      except PermissionError:
        return False
      except ProcessLookupError:
        return True
      return False
    while True:
      try:
        process_id, _ = os.waitpid(-1, os.WNOHANG)
      except ChildProcessError:
        return True
      if process_id == 0:
        return False

  def _signal(self, signal_number: int) -> None:
    """Send `signal_number` to the command's group and to the descendants this
    process can see: under a kill to each of them, otherwise to each outside the
    group. Report each process a kill may not reach."""
    group = self._process.pid
    # an unreaped command holds its pid, so its group id names no other group
    pinned = self._process.returncode is None
    refusers: list[str] = []
    if not self._subreaper:
      if _refused(os.killpg, group, signal_number):
        refusers.append(f'process group {group}')
      if pinned and signal_number == signal.SIGKILL and _refused(os.kill, group, signal_number):
        refusers.append(f'pid {group}')
    else:
      if pinned:
        # a process forking while the descendants are listed escapes the
        # listing, but not a signal to its group
        with contextlib.suppress(ProcessLookupError, PermissionError):
          os.killpg(group, signal_number)
      for descendant in _descendants():
        if signal_number == signal.SIGKILL or not pinned or descendant.group_id != group:
          if _refused(os.kill, descendant.process_id, signal_number):
            refusers.append(f'pid {descendant.process_id} ({_printable(descendant.name)})')
    if signal_number == signal.SIGKILL:
      for refuser in refusers:
        self._report_refusal(refuser)

  def _report_refusal(self, refuser: str) -> None:
    """Name `refuser` in the output once: the kill waits for it to exit."""
    if refuser in self._reported:
      return
    self._reported.add(refuser)
    self._report_stream.write(
      f'[job-supervisor] may not kill {refuser}; waiting for it to exit\n'.encode()
    )
    self._report_stream.flush()


def end() -> None:
  """End what this process supervises: ask for a SIGTERM, then for a SIGKILL
  once the grace passes without the supervision ending."""
  os.kill(os.getpid(), signal.SIGTERM)
  if not _ended.wait(_TERM_GRACE_SECONDS):
    os.kill(os.getpid(), KILL_SIGNAL)


def _watch_owner(owner_fd: int) -> None:
  with os.fdopen(owner_fd, 'rb') as owner:
    while owner.read(1) != b'':
      pass
  end()


def supervise(
  argv: Sequence[str],
  ready_fd: int,
  owner_fd: int,
  *,
  output: Optional[IO[bytes]] = None,
  finished: Optional[Callable[[int], None]] = None,
) -> int:
  """Run `argv` in a process group of its own until it and every descendant exit.

  SIGTERM asks the supervisor to terminate them and `KILL_SIGNAL` to kill them,
  and the owner handle closing asks for both through `end`; a kill names in the
  output each process it may not kill and waits for it to exit. It then dies of
  the signal it was asked for or the command died of, and otherwise returns the
  command's exit code."""
  supervision = _Supervision(argv, output, _become_subreaper())
  threading.Thread(target=_watch_owner, args=(owner_fd,), daemon=True).start()
  with os.fdopen(ready_fd, 'wb', buffering=0) as ready:
    with contextlib.suppress(BrokenPipeError):
      ready.write(b'1')
  status = supervision.wait()
  _ended.set()
  exit_signal = supervision.requested
  if exit_signal is None and status < 0:
    exit_signal = -status
  result = -exit_signal if exit_signal is not None else status
  if finished is not None:
    finished(result)
  if exit_signal is not None:
    if exit_signal not in {signal.SIGKILL, signal.SIGSTOP}:
      signal.signal(exit_signal, signal.SIG_DFL)
    os.kill(os.getpid(), exit_signal)
    raise RuntimeError('termination signal did not end the supervisor')
  return status


def _run() -> int:
  if len(sys.argv) < 4:
    raise ValueError('usage: python -m bro.job_supervisor READY_FD OWNER_FD ARGV...')
  return supervise(sys.argv[3:], int(sys.argv[1]), int(sys.argv[2]))


if __name__ == '__main__':
  sys.exit(_run())
