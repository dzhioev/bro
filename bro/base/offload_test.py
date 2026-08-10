import asyncio
import sys
import threading
import time

import pytest

from bro.base import spawn
from bro.base.offload import off_loop


@pytest.mark.asyncio
async def test_returns_the_call_result() -> None:
  assert await off_loop(str.upper, 'hi') == 'HI'


@pytest.mark.asyncio
async def test_passes_keyword_arguments() -> None:
  assert await off_loop(sorted, [3, 1, 2], reverse=True) == [3, 2, 1]


@pytest.mark.asyncio
async def test_propagates_the_call_exception() -> None:
  def fail() -> None:
    raise ValueError('nope')

  with pytest.raises(ValueError, match='nope'):
    await off_loop(fail)


@pytest.mark.asyncio
async def test_runs_in_another_thread_leaving_the_loop_free() -> None:
  ticks = 0

  async def tick() -> None:
    nonlocal ticks
    while True:
      ticks += 1
      await asyncio.sleep(0.01)

  ticker = asyncio.create_task(tick())
  thread_id = await off_loop(lambda: (time.sleep(0.2), threading.get_ident())[1])
  ticker.cancel()
  assert thread_id != threading.get_ident()
  assert ticks > 1


@pytest.mark.asyncio
async def test_cancelling_the_await_leaves_the_call_running() -> None:
  # the abandonment contract: the thread is not killed, so whatever the call
  # holds is the caller's to release.
  finished = threading.Event()
  task = asyncio.create_task(off_loop(lambda: (time.sleep(0.2), finished.set())))
  await asyncio.sleep(0.05)
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert finished.wait(timeout=5)


def test_cancelled_call_does_not_delay_process_exit() -> None:
  # the reason this exists instead of asyncio.to_thread: the default executor's
  # threads are joined at interpreter shutdown, so the same script over
  # to_thread exits only after the abandoned sleep returns.
  script = (
    'import asyncio, sys, time\n'
    f'sys.path.insert(0, {str(spawn.__file__).rsplit("/bro/base/", 1)[0]!r})\n'
    'from bro.base.offload import off_loop\n'
    'async def main():\n'
    '  task = asyncio.create_task(off_loop(time.sleep, 30))\n'
    '  await asyncio.sleep(0.1)\n'
    '  task.cancel()\n'
    '  try:\n'
    '    await task\n'
    '  except asyncio.CancelledError:\n'
    '    pass\n'
    'asyncio.run(main())\n'
  )
  started = time.monotonic()
  spawn.run([sys.executable, '-c', script], timeout=30, check=True)
  assert time.monotonic() - started < 10
