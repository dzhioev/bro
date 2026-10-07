"""Process jobs with one consumable output cursor and exit record."""

import atexit
import codecs
import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

from bro.base import spawn
from bro.base.text_window import DEFAULT_LIMIT, apply_limit, format_size, take_head

if TYPE_CHECKING:
  from bro.inbox import Inbox

JobMode = Literal['fg', 'bg']

TERM_GRACE_SECONDS = 5.0
SPOOL_MEMORY_BYTES = 1_000_000
_READ_CHUNK_BYTES = 65_536


@dataclass(frozen=True)
class Notification:
  job_id: str
  mode: JobMode
  command: str
  lines: str
  exit_code: Optional[int] = None


@dataclass(frozen=True)
class JobStatus:
  id: str
  mode: JobMode
  command: str
  state: Literal['running', 'exited']
  exit_code: Optional[int]
  unread_lines: int


def _pending_marker(remainder: str, clamp_note: str) -> Optional[str]:
  if len(remainder) == 0:
    return f'[...{clamp_note}...]' if len(clamp_note) > 0 else None
  body = f'{len(remainder.splitlines()):,} lines / {format_size(len(remainder))}'
  suffix = f' — {clamp_note}' if len(clamp_note) > 0 else ''
  return f'[...pending: {body}{suffix}...]'


class Job:
  """One process, its spool, and the cursor shared by every output consumer."""

  def __init__(
    self,
    job_id: str,
    command: str,
    mode: JobMode = 'bg',
    *,
    inbox: Optional['Inbox'] = None,
    spool_memory_bytes: int = SPOOL_MEMORY_BYTES,
  ):
    if mode not in {'fg', 'bg'}:
      raise ValueError(f'unknown job mode {mode!r}')
    self.id = job_id
    self.command = command
    self.mode: JobMode = mode
    self._inbox = inbox
    self._condition = threading.Condition()
    self._spool = tempfile.SpooledTemporaryFile(
      max_size=spool_memory_bytes, mode='w+', encoding='utf-8', newline=''
    )
    self._drained = False
    self._returncode: Optional[int] = None
    self._settled = False
    self._exit_consumed = False
    self._cursor = self._spool.tell()
    ready_read_fd, ready_write_fd = os.pipe()
    owner_read_fd, self._owner_write_fd = os.pipe()
    with (
      os.fdopen(ready_read_fd, 'rb') as ready_reader,
      os.fdopen(ready_write_fd, 'wb') as ready_writer,
      os.fdopen(owner_read_fd, 'rb') as owner_reader,
    ):
      self.process = spawn.popen(
        [
          sys.executable,
          '-m',
          'bro.job_supervisor',
          str(ready_write_fd),
          str(owner_read_fd),
          command,
        ],
        pass_fds=(ready_write_fd, owner_read_fd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
      )
      ready_writer.close()
      owner_reader.close()
      if ready_reader.read(1) != b'1':
        self.process.wait()
        stdout = self.process.stdout
        assert stdout is not None
        failure = stdout.read().decode(errors='replace').strip()
        stdout.close()
        os.close(self._owner_write_fd)
        self._owner_write_fd = -1
        self._spool.close()
        raise RuntimeError(f'job supervisor failed to start: {failure}')
    self._reader_thread = threading.Thread(target=self._drain, daemon=True)
    self._exit_thread = threading.Thread(target=self._record_exit, daemon=True)
    self._reader_thread.start()
    self._exit_thread.start()

  def _drain(self) -> None:
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    stdout = self.process.stdout
    assert stdout is not None
    while True:
      chunk = os.read(stdout.fileno(), _READ_CHUNK_BYTES)
      if len(chunk) == 0:
        break
      self._append(decoder.decode(chunk))
    self._append(decoder.decode(b'', final=True))
    stdout.close()
    with self._condition:
      self._drained = True
    self._signal_exit_if_finished()

  def _append(self, text: str) -> None:
    if len(text) == 0:
      return
    with self._condition:
      self._spool.seek(0, os.SEEK_END)
      self._spool.write(text)
      self._spool.flush()
      self._condition.notify_all()

  def _record_exit(self) -> None:
    returncode = self.process.wait()
    with self._condition:
      self._returncode = returncode
      owner_write_fd = self._owner_write_fd
      self._owner_write_fd = -1
    if owner_write_fd >= 0:
      os.close(owner_write_fd)
    self._signal_exit_if_finished()

  def _signal_exit_if_finished(self) -> None:
    with self._condition:
      process_finished = self._process_finished_locked()
      reports_exit = not self._exit_consumed
      already_settled = self._settled
    if not process_finished or already_settled:
      return
    if reports_exit:
      self._mark_news()
    with self._condition:
      self._settled = True
      self._condition.notify_all()

  def _mark_news(self) -> None:
    if self._inbox is not None:
      self._inbox.mark(self)

  def _state_line_locked(self) -> str:
    return 'running' if self._returncode is None else f'exited (code {self._returncode})'

  def _process_finished_locked(self) -> bool:
    return self._returncode is not None and self._drained

  def _finished_locked(self) -> bool:
    return self._process_finished_locked() and self._settled

  def _end_locked(self) -> int:
    self._spool.seek(0, os.SEEK_END)
    return self._spool.tell()

  def _unread_locked(self) -> str:
    self._spool.seek(self._cursor)
    return self._spool.read()

  def _advance_locked(self, character_count: int) -> None:
    self._spool.seek(self._cursor)
    self._spool.read(character_count)
    self._cursor = self._spool.tell()

  def _has_news_locked(self) -> bool:
    exited = self._process_finished_locked() and not self._exit_consumed
    return exited

  def has_news(self) -> bool:
    with self._condition:
      return self._has_news_locked()

  def become_background(self) -> None:
    with self._condition:
      if self.mode != 'fg':
        raise ValueError(f'{self.id} is {self.mode}, not foreground')
      self.mode = 'bg'
      has_news = self._has_news_locked()
      self._condition.notify_all()
    if has_news:
      self._mark_news()

  def wait_finished(self, deadline: float, cancelled: threading.Event) -> bool:
    """Wait for the exit and complete output drain without consuming either."""
    with self._condition:
      while not self._finished_locked() and not cancelled.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          return False
        self._condition.wait(remaining)
      return self._finished_locked()

  def poll(self, limit: int = DEFAULT_LIMIT, *, tail: bool = False) -> str:
    """Read the current unread head or tail without blocking."""
    with self._condition:
      pending = self._unread_locked()
      if tail:
        self._cursor = self._end_locked()
        if len(pending) == 0:
          return self._state_line_locked()
        return f'{self._state_line_locked()}\n{apply_limit(pending, limit, keep="tail")}'
      if len(pending) == 0:
        return self._state_line_locked()
      kept, clamp_note = take_head(pending, limit)
      self._advance_locked(len(kept))
      pieces = [self._state_line_locked(), kept.rstrip('\n')]
      marker = _pending_marker(pending[len(kept) :], clamp_note)
      if marker is not None:
        pieces.append(marker)
      return '\n'.join(pieces)

  def status(self) -> JobStatus:
    with self._condition:
      unread = self._unread_locked()
      return JobStatus(
        id=self.id,
        mode=self.mode,
        command=self.command,
        state='running' if self._returncode is None else 'exited',
        exit_code=self._returncode,
        unread_lines=len(unread.splitlines()),
      )

  def settle_foreground(self, limit: int = DEFAULT_LIMIT) -> tuple[str, bool]:
    """Consume a finished foreground result or atomically move a live job to background."""
    with self._condition:
      if self.mode != 'fg':
        raise ValueError(f'{self.id} is {self.mode}, not foreground')
      section = self._unread_locked()
      self._cursor = self._end_locked()
      if self._finished_locked():
        if self._exit_consumed:
          raise RuntimeError(f'{self.id} exit was already consumed')
        self._exit_consumed = True
        state = self._state_line_locked()
        result = (
          state if len(section) == 0 else f'{state}\n{apply_limit(section, limit, keep="tail")}'
        )
        return result, False
      self.mode = 'bg'
      self._condition.notify_all()
      result = (
        'running' if len(section) == 0 else f'running\n{apply_limit(section, limit, keep="tail")}'
      )
      return result, True

  def foreground_result(self, limit: int = DEFAULT_LIMIT) -> Optional[str]:
    """Consume a finished foreground job's exit and tail, or return None while live."""
    with self._condition:
      if not self._finished_locked():
        return None
    result, became_background = self.settle_foreground(limit)
    assert not became_background
    return result

  def drain_notification(self, limit: int = DEFAULT_LIMIT) -> Optional[Notification]:
    with self._condition:
      if not self._has_news_locked():
        return None
      section = self._unread_locked()
      self._cursor = self._end_locked()
      self._exit_consumed = True
      return Notification(
        self.id,
        self.mode,
        self.command,
        apply_limit(section, limit, keep='tail'),
        exit_code=self._returncode,
      )

  def kill(self, *, grace_seconds: float = TERM_GRACE_SECONDS) -> str:
    """Terminate the process group and consume the exit record exactly once."""
    already_exited = self.process.poll() is not None
    if not already_exited:
      spawn.terminate_group(self.process)
      deadline = time.monotonic() + max(grace_seconds, 0.0)
      with self._condition:
        while self._returncode is None:
          remaining = deadline - time.monotonic()
          if remaining <= 0:
            break
          self._condition.wait(remaining)
        needs_kill = self._returncode is None
      if needs_kill:
        spawn.kill_group(self.process)
    with self._condition:
      while self._returncode is None:
        self._condition.wait()
      returncode = self._returncode
      self._exit_consumed = True
      self._condition.notify_all()
    prefix = 'already exited' if already_exited else 'exited'
    return f'{self.id} {prefix} (code {returncode})'

  def close(self) -> None:
    if self.process.poll() is None:
      spawn.kill_group(self.process)
    with self._condition:
      while self._returncode is None:
        self._condition.wait()
    self._exit_thread.join()
    self._reader_thread.join()
    with self._condition:
      self._exit_consumed = True
      self._spool.close()


class Registry:
  """A lifetime-scoped job table."""

  def __init__(self, inbox: Optional['Inbox'] = None):
    self.inbox = inbox
    self._lock = threading.Lock()
    self._jobs: dict[str, Job] = {}
    self._counter = 0
    self._closed = False
    atexit.register(self.close)

  @property
  def closed(self) -> bool:
    with self._lock:
      return self._closed

  def start(self, command: str, mode: JobMode = 'bg') -> Job:
    with self._lock:
      if self._closed:
        raise RuntimeError('job registry is closed')
      self._counter += 1
      job = Job(f'job-{self._counter}', command, mode, inbox=self.inbox)
      self._jobs[job.id] = job
    return job

  def get(self, job_id: str) -> Job:
    with self._lock:
      job = self._jobs.get(job_id)
      known = ', '.join(self._jobs) if len(self._jobs) > 0 else '(none)'
    if job is None:
      raise ValueError(f'unknown job id {job_id!r}; known jobs: {known}')
    return job

  def values(self) -> list[Job]:
    with self._lock:
      return list(self._jobs.values())

  def close(self) -> None:
    with self._lock:
      if self._closed:
        return
      self._closed = True
      jobs = list(self._jobs.values())
      self._jobs.clear()
    atexit.unregister(self.close)
    with contextlib.ExitStack() as stack:
      for job in jobs:
        stack.callback(job.close)
