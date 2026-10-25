import asyncio
import threading

import pytest

from bro.files.confinement import SandboxLimits
from bro.files.slots import WorkerSlots, memory_budget_bytes


def test_construction_needs_no_event_loop():
  slots = WorkerSlots(budget_bytes=SandboxLimits().address_space_bytes)

  async def call():
    with await slots.acquire():
      pass

  asyncio.run(call())


@pytest.mark.asyncio
async def test_no_slot_fails_by_name_instead_of_waiting():
  slots = WorkerSlots(budget_bytes=123)
  with pytest.raises(RuntimeError, match=r'memory budget of 123 bytes cannot hold one worker of'):
    await slots.acquire()


@pytest.mark.asyncio
async def test_queue_bound_and_release_on_exception():
  slots = WorkerSlots(SandboxLimits(address_space_bytes=100), budget_bytes=199, queue_seconds=0.01)
  with pytest.raises(LookupError), await slots.acquire():
    with pytest.raises(RuntimeError, match='every slot is busy'):
      await slots.acquire()
    raise LookupError()
  with await slots.acquire():
    pass


@pytest.mark.asyncio
async def test_transferred_lease_is_held_until_the_read_thread_releases_it():
  slots = WorkerSlots(budget_bytes=SandboxLimits().address_space_bytes, queue_seconds=0.01)
  lease = await slots.acquire()
  with lease:
    lease.transfer()
  with pytest.raises(RuntimeError, match='every slot is busy'):
    await slots.acquire()
  thread = threading.Thread(target=lease.release)
  thread.start()
  thread.join()
  with await slots.acquire():
    pass


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_consume_a_slot(monkeypatch):
  slots = WorkerSlots(budget_bytes=SandboxLimits().address_space_bytes)
  started = asyncio.Event()
  original = slots._wait

  async def observed_wait(waiter):
    started.set()
    return await original(waiter)

  with await slots.acquire():
    monkeypatch.setattr(slots, '_wait', observed_wait)
    waiting = asyncio.create_task(slots.acquire())
    await started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
      await waiting
  with await slots.acquire():
    pass


def test_budget_uses_finite_cgroup_and_falls_back_to_physical_memory(monkeypatch):
  from bro.files import slots

  monkeypatch.setattr(slots.Path, 'read_text', lambda self: '1000')
  assert memory_budget_bytes() == 500
  monkeypatch.setattr(slots.Path, 'read_text', lambda self: 'max')
  monkeypatch.setattr(slots.os, 'sysconf', lambda name: 20 if name == 'SC_PHYS_PAGES' else 100)
  assert memory_budget_bytes() == 1000
  monkeypatch.setattr(slots.Path, 'read_text', lambda self: str(100 * 1024**3))
  assert memory_budget_bytes() == slots.MAX_MEMORY_BUDGET_BYTES


def test_budget_respects_process_cgroup_and_its_finite_ancestors(monkeypatch):
  from bro.files import slots

  files = {
    '/proc/self/cgroup': '0::/service/workers\n',
    '/sys/fs/cgroup/memory.max': 'max',
    '/sys/fs/cgroup/service/memory.max': '1000',
    '/sys/fs/cgroup/service/workers/memory.max': '2000',
  }
  monkeypatch.setattr(slots.Path, 'read_text', lambda path: files[str(path)])
  assert memory_budget_bytes() == 500


@pytest.mark.asyncio
async def test_pool_budget_is_shared_across_event_loops_in_different_threads():
  from contextlib import ExitStack

  from bro.base.offload import off_loop

  slots = WorkerSlots(budget_bytes=SandboxLimits().address_space_bytes, queue_seconds=0.01)
  entered = asyncio.Event()
  release = threading.Event()
  loop = asyncio.get_running_loop()

  async def hold():
    with await slots.acquire():
      loop.call_soon_threadsafe(entered.set)
      await off_loop(release.wait)

  thread = threading.Thread(target=lambda: asyncio.run(hold()))
  with ExitStack() as stack:
    thread.start()
    stack.callback(thread.join)
    stack.callback(release.set)
    await entered.wait()
    with pytest.raises(RuntimeError, match='every slot is busy'):
      await slots.acquire()
    release.set()
    thread.join()
    with await slots.acquire():
      pass
