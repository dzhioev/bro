"""Lazy, memory-budgeted admission held across reads and workers."""

import asyncio
import os
import threading
from pathlib import Path
from types import TracebackType
from typing import Self

from bro.files.confinement import SandboxLimits

QUEUE_SECONDS = 30
READ_SECONDS = 10
MAX_MEMORY_BUDGET_BYTES = 4 * 1024**3


def memory_budget_bytes() -> int:
  root = Path('/sys/fs/cgroup')
  paths = [root / 'memory.max']
  try:
    membership = Path('/proc/self/cgroup').read_text()
  except OSError:
    membership = ''
  for line in membership.splitlines():
    if line.startswith('0::'):
      group = root / line[3:].lstrip('/')
      while group != root and group.is_relative_to(root):
        paths.append(group / 'memory.max')
        group = group.parent
  finite = []
  for path in paths:
    try:
      value = path.read_text().strip()
    except OSError:
      continue
    if value != 'max':
      memory = int(value)
      if memory <= 0:
        raise ValueError('cgroup memory.max must be positive')
      finite.append(memory)
  memory = min(finite) if finite else os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
  return min(MAX_MEMORY_BUDGET_BYTES, memory // 2)


class SlotLease:
  def __init__(self, semaphore: asyncio.Semaphore, loop: asyncio.AbstractEventLoop):
    self._semaphore = semaphore
    self._loop = loop
    self._thread = threading.get_ident()
    self._lock = threading.Lock()
    self._released = False
    self._transferred = False

  def __enter__(self) -> Self:
    return self

  def __exit__(
    self,
    kind: type[BaseException] | None,
    error: BaseException | None,
    traceback: TracebackType | None,
  ) -> None:
    if not self._transferred:
      self.release()

  def transfer(self) -> None:
    """Move release responsibility to an abandoned read's thread."""
    self._transferred = True

  def release(self) -> None:
    """Release exactly once, including when called by a read thread."""
    with self._lock:
      if self._released:
        return
      self._released = True
    if threading.get_ident() == self._thread:
      self._semaphore.release()
    else:
      self._loop.call_soon_threadsafe(self._semaphore.release)


class WorkerSlots:
  def __init__(
    self,
    limits: SandboxLimits | None = None,
    *,
    budget_bytes: int | None = None,
    queue_seconds: float = QUEUE_SECONDS,
  ):
    if budget_bytes is not None and (type(budget_bytes) is not int or budget_bytes < 0):
      raise ValueError('budget_bytes must be a nonnegative integer')
    if not 0 < queue_seconds < float('inf'):
      raise ValueError('queue_seconds must be finite and positive')
    self.limits = limits or SandboxLimits()
    self.budget_bytes = budget_bytes
    self.queue_seconds = queue_seconds
    self._semaphore: asyncio.Semaphore | None = None
    self._loop: asyncio.AbstractEventLoop | None = None

  async def acquire(self) -> SlotLease:
    loop = asyncio.get_running_loop()
    if self._semaphore is None:
      if self.budget_bytes is None:
        self.budget_bytes = memory_budget_bytes()
      self._semaphore = asyncio.Semaphore(self.budget_bytes // self.limits.address_space_bytes)
      self._loop = loop
    if loop is not self._loop:
      raise RuntimeError('file worker slots belong to another event loop')
    assert self.budget_bytes is not None
    if self.budget_bytes < self.limits.address_space_bytes:
      raise RuntimeError(
        f'file sandbox unavailable: the memory budget of {self.budget_bytes} bytes '
        f'cannot hold one worker of {self.limits.address_space_bytes} bytes'
      )
    try:
      async with asyncio.timeout(self.queue_seconds):
        await self._semaphore.acquire()
    except TimeoutError:
      raise RuntimeError('file worker queue timed out: every slot is busy')
    return SlotLease(self._semaphore, loop)
