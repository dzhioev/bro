"""job launch — a host command run as the answering process of an exchange.

A job is the third answer shape: the host launches a process that does not
speak the protocol, observes it, and speaks on its behalf — no channel is
provisioned, and the outcome is derived from how the process ended
(`Dispatcher.job`). The command runs in its own process group so a kill takes
whatever children it spawned along, and its merged output is retained in a
bounded ring for the result's tail.
"""

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass

from bro.broker.spawn import ChildHandle, RingBuffer

DEFAULT_RING_BYTES = 1 << 16  # 64 KiB — a full traceback + context, bounded

_DRAIN_CHUNK = 65536

# seconds a killed job gets to end on SIGTERM before the group is SIGKILLed —
# room for a command supervising resources of its own (containers, temp state)
# to tear them down
_TERM_GRACE = 10.0


@dataclass(frozen=True)
class CommandJob:
  """one host command as a job: what to run, where, and under which environment.

  `env` is the process's full environment — an explicit snapshot, never a live
  `os.environ` read."""

  command: tuple[str, ...]
  cwd: str
  env: dict[str, str]


class _CommandHandle(ChildHandle):
  def __init__(self, process: asyncio.subprocess.Process, ring_bytes: int):
    self._process = process
    self._ring = RingBuffer(ring_bytes)
    self._drain = asyncio.create_task(self._drain_output())

  async def _drain_output(self) -> None:
    assert self._process.stdout is not None  # carries stderr too (merged at spawn)
    while True:
      chunk = await self._process.stdout.read(_DRAIN_CHUNK)
      if len(chunk) == 0:
        return
      self._ring.write(chunk)

  def _signal_group(self, signum: int) -> None:
    # the group can be gone already (the process exited between the caller's
    # check and this signal) — that is the kill succeeding, not an error
    with contextlib.suppress(ProcessLookupError):
      os.killpg(self._process.pid, signum)

  async def wait(self) -> int:
    code = await self._process.wait()
    await self._drain  # let the final output land in the ring before tail() is read
    return code

  async def kill(self) -> None:
    if self._process.returncode is not None:
      return
    self._signal_group(signal.SIGTERM)
    try:
      await asyncio.wait_for(self._process.wait(), _TERM_GRACE)
    except TimeoutError:
      self._signal_group(signal.SIGKILL)
      await self._process.wait()

  def output_tail(self) -> str:
    return self._ring.tail().decode('utf-8', errors='replace')


async def launch(job: CommandJob, *, ring_bytes: int = DEFAULT_RING_BYTES) -> ChildHandle:
  """start the job's process — its own session (= process group), merged output
  captured — and return the handle the Runtime supervises."""
  process = await asyncio.create_subprocess_exec(
    *job.command,
    cwd=job.cwd,
    env=job.env,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT,
    start_new_session=True,
  )
  return _CommandHandle(process, ring_bytes)
