"""background jobs for the dev toolset: start, watch, kill.

A `Job` wraps one background process started with `bash -c`: stdout and stderr
merge into a single chronological stream that a reader thread drains continuously
into an in-memory spool, so the child never blocks on a full pipe, and a second
thread records the exit code the moment the child dies. `Job.watch` reads the
spool through a per-job cursor and owns the two read modes (incremental
pagination and tail). `Registry` tracks jobs for the process lifetime and
group-kills the still-running ones when the hosting server closes, with
interpreter exit as the backstop.
"""

import atexit
import codecs
import contextlib
import io
import os
import subprocess
import threading
import time
from collections.abc import Generator
from typing import Optional

from bro.base import spawn
from bro.base.text_window import apply_limit, format_size, take_head

# seconds a killed job's process group gets to exit on SIGTERM before the
# escalation to SIGKILL.
TERM_GRACE_SECONDS = 5.0

_READ_CHUNK_BYTES = 65536


def _pending_marker(remainder: str, clamp_note: str) -> Optional[str]:
  # the incremental counterpart of apply_limit's skipped-after marker: the
  # remainder is not dropped, it stays spooled for the next watch call.
  if len(remainder) == 0:
    return f'[...{clamp_note}...]' if len(clamp_note) > 0 else None
  body = f'{len(remainder.splitlines()):,} lines / {format_size(len(remainder))}'
  suffix = f' — {clamp_note}' if len(clamp_note) > 0 else ''
  return f'[...pending: {body}{suffix}...]'


class Job:
  """one background process plus its spool, read cursor, and exit record."""

  def __init__(self, job_id: str, command: str):
    self.id = job_id
    self.command = command
    # guards the spool, the drained flag, and the exit record; the reader and
    # exit-recorder threads notify on every change so a blocked watch wakes.
    self._condition = threading.Condition()
    self._spool = io.StringIO()
    self._drained = False
    self._returncode: Optional[int] = None
    self._cursor = 0
    self._watching = False
    self.process = spawn.popen(
      ['bash', '-c', command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    # daemon threads: at interpreter exit they may still be blocked on a pipe a
    # grandchild holds open, and non-daemon threads are joined before atexit
    # hooks run — the registry's group-kill must not wait on them.
    threading.Thread(target=self._drain, daemon=True).start()
    threading.Thread(target=self._record_exit, daemon=True).start()

  def _drain(self) -> None:
    # os.read returns as soon as any bytes arrive (a TextIOWrapper.read(n) would
    # block until n characters), and the incremental decoder keeps multi-byte
    # characters split across chunk boundaries intact.
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
      self._condition.notify_all()

  def _append(self, text: str) -> None:
    if len(text) == 0:
      return
    with self._condition:
      self._spool.write(text)
      self._condition.notify_all()

  def _record_exit(self) -> None:
    returncode = self.process.wait()
    with self._condition:
      self._returncode = returncode
      self._condition.notify_all()

  def _state_line(self) -> str:
    return 'running' if self._returncode is None else f'exited (code {self._returncode})'

  def _finished(self) -> bool:
    # exited AND drained: the exit code alone can precede the last spool chunks
    # (the reader may be one os.read behind), so a final collect waits for both.
    return self._returncode is not None and self._drained

  @contextlib.contextmanager
  def _claimed(self) -> Generator[None]:
    # the claim is condition-guarded state like the spool and the exit record, so
    # both edges notify and a caller can wait for the job to be taken or freed.
    with self._condition:
      if self._watching:
        raise ValueError(
          f'{self.id} is already being watched; watch is exclusive per job — the running '
          'call holds the job for its whole wait, retry after it returns'
        )
      self._watching = True
      self._condition.notify_all()
    try:
      yield
    finally:
      with self._condition:
        self._watching = False
        self._condition.notify_all()

  def watch(
    self,
    *,
    wait_seconds: float,
    limit: int,
    tail: bool,
    woken: Optional[threading.Event] = None,
  ) -> str:
    """blocking read of the job — call off-loop. Exclusive per job: the claim is
    held for the whole call, wait included, and a concurrent same-job watch fails
    immediately rather than racing for stream slices. `kill` claims nothing — the
    exit it forces ends a blocked watch, as does `wake` on the `woken` event a
    caller passes here to keep a handle on the call it is about to start."""
    if woken is None:
      woken = threading.Event()
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    with self._claimed(), self._condition:
      if tail:
        return self._watch_tail(deadline, limit, woken)
      return self._watch_incremental(deadline, limit, woken)

  def wake(self, woken: threading.Event) -> None:
    """end the watch handed `woken` now, as if its window had elapsed. The event is
    that one call's, so a wake landing before the call starts still ends it: the
    claim must not outlive its caller's interest, which for the large windows an
    iterative watcher passes would leave the job unwatchable for minutes."""
    woken.set()
    with self._condition:
      self._condition.notify_all()

  def _watch_incremental(self, deadline: float, limit: int, woken: threading.Event) -> str:
    # decision order: pending backlog → return it immediately; exited and drained
    # → immediate bare state line; otherwise block for output or exit, a quiet
    # window ending in a bare state-line heartbeat.
    while True:
      pending = self._spool.getvalue()[self._cursor :]
      if len(pending) > 0:
        return self._emit_slice(pending, limit)
      if self._finished():
        return self._state_line()
      remaining = deadline - time.monotonic()
      if remaining <= 0 or woken.is_set():
        return self._state_line()
      self._condition.wait(remaining)

  def _emit_slice(self, pending: str, limit: int) -> str:
    kept, clamp_note = take_head(pending, limit)
    self._cursor += len(kept)
    pieces = [self._state_line(), kept.rstrip('\n')]
    marker = _pending_marker(pending[len(kept) :], clamp_note)
    if marker is not None:
      pieces.append(marker)
    return '\n'.join(pieces)

  def _watch_tail(self, deadline: float, limit: int, woken: threading.Event) -> str:
    # only exit (fully drained) or the window's end wakes this mode; every return
    # jumps the cursor to the spool end and keeps the tail of what was jumped —
    # on exit the final diagnostics, on timeout a progress glimpse.
    while True:
      remaining = deadline - time.monotonic()
      if self._finished() or remaining <= 0 or woken.is_set():
        value = self._spool.getvalue()
        section = value[self._cursor :]
        self._cursor = len(value)
        if len(section) == 0:
          return self._state_line()
        return f'{self._state_line()}\n{apply_limit(section, limit, keep="tail")}'
      self._condition.wait(remaining)

  def kill(self, *, grace_seconds: float = TERM_GRACE_SECONDS) -> str:
    """blocking terminate — call off-loop. SIGTERMs the whole process group,
    escalating to SIGKILL after `grace_seconds`. The record and spool stay
    readable afterwards for a final collect."""
    with self._condition:
      if self._returncode is not None:
        return f'{self.id} already exited (code {self._returncode})'
    spawn.terminate_group(self.process)
    try:
      self.process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
      spawn.kill_group(self.process)
      self.process.wait()
    return f'{self.id} exited (code {self.process.returncode})'


class Registry:
  """in-process job table for one toolset instance."""

  def __init__(self):
    self._lock = threading.Lock()
    self._jobs: dict[str, Job] = {}
    self._counter = 0
    atexit.register(self.close)

  def start(self, command: str) -> Job:
    with self._lock:
      self._counter += 1
      job = Job(f'job-{self._counter}', command)
      self._jobs[job.id] = job
    return job

  def get(self, job_id: str) -> Job:
    with self._lock:
      job = self._jobs.get(job_id)
      known = ', '.join(self._jobs) if len(self._jobs) > 0 else '(none)'
    if job is None:
      raise ValueError(f'unknown job id {job_id!r}; known jobs: {known}')
    return job

  def close(self) -> None:
    """group-kill every still-running job. The hosting server's teardown calls
    it when the session ends; the atexit registration is the backstop for a
    process that never gets there."""
    for job in list(self._jobs.values()):
      if job.process.poll() is None:
        spawn.kill_group(job.process)
