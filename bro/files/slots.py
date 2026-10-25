"""Lazy, memory-budgeted admission held across reads and workers."""

import asyncio
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
  def __init__(self, release: Callable[[], None]):
    self._release = release
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
    self._release()


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
    self._lock = threading.Lock()
    self._available: int | None = None
    self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = set()

  @contextmanager
  def _waiting(self) -> Iterator[asyncio.Future[None] | None]:
    loop = asyncio.get_running_loop()
    waiter = None
    with self._lock:
      if self._available is None:
        if self.budget_bytes is None:
          self.budget_bytes = memory_budget_bytes()
        if self.budget_bytes < self.limits.address_space_bytes:
          raise RuntimeError(
            f'file sandbox unavailable: the memory budget of {self.budget_bytes} bytes '
            f'cannot hold one worker of {self.limits.address_space_bytes} bytes'
          )
        self._available = self.budget_bytes // self.limits.address_space_bytes
      if self._available:
        self._available -= 1
      else:
        waiter = loop.create_future()
        self._waiters.add((loop, waiter))
    try:
      yield waiter
    finally:
      if waiter is not None:
        with self._lock:
          self._waiters.remove((loop, waiter))

  @staticmethod
  async def _wait(waiter: asyncio.Future[None]) -> None:
    await waiter

  def _release(self) -> None:
    with self._lock:
      assert self._available is not None
      self._available += 1
      for loop, waiter in self._waiters:
        loop.call_soon_threadsafe(self._wake, waiter)

  @staticmethod
  def _wake(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
      waiter.set_result(None)

  async def acquire(self) -> SlotLease:
    try:
      async with asyncio.timeout(self.queue_seconds):
        while True:
          with self._waiting() as waiter:
            if waiter is None:
              return SlotLease(self._release)
            await self._wait(waiter)
    except TimeoutError:
      raise RuntimeError('file worker queue timed out: every slot is busy')


default_slots = WorkerSlots()
