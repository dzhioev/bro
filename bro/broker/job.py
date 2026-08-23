"""job launch — a host command run as the answering process of an exchange.

A job is the third answer shape: the host launches a process that does not
speak the protocol, observes it, and speaks on its behalf — no channel is
provisioned, and the outcome is derived from how the process ended
(`Dispatcher.job`). The command runs in its own process group so a kill takes
whatever children it spawned along.

Its whole run is collected into one directory, which is also the process's
working directory: `stdout` and `stderr` hold the two streams verbatim,
`status.json` records how the process ended, and `output/` is where the command
writes whatever it wants the requester to receive. That directory is the job's
answer, so nothing of the run is buffered for a tail.
"""

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bro.broker.spawn import ChildHandle

# the fixed layout of a run directory
OUTPUT_DIRECTORY = 'output'  # the job's own files, the ones the requester receives
STDOUT_FILE = 'stdout'
STDERR_FILE = 'stderr'
STATUS_FILE = 'status.json'

# seconds a killed job gets to end on SIGTERM before the group is SIGKILLed —
# room for a command supervising resources of its own (containers, temp state)
# to tear them down
_TERM_GRACE = 10.0


@dataclass(frozen=True)
class CommandJob:
  """one host command as a job: what to run and under which environment.

  `env` is the process's full environment — an explicit snapshot, never a live
  `os.environ` read. Where it runs is not the command's to choose: a job's cwd
  is the run directory it answers with."""

  command: tuple[str, ...]
  env: dict[str, str]


class _CommandHandle(ChildHandle):
  def __init__(self, process: asyncio.subprocess.Process):
    self._process = process

  def _signal_group(self, signum: int) -> None:
    # the group can be gone already (the process exited between the caller's
    # check and this signal) — that is the kill succeeding, not an error
    with contextlib.suppress(ProcessLookupError):
      os.killpg(self._process.pid, signum)

  async def wait(self) -> int:
    return await self._process.wait()

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
    return ''


async def launch(job: CommandJob, directory: Path) -> ChildHandle:
  """start the job's process in `directory` — its own session (= process group),
  each stream written to its run file — and return the handle the Runtime
  supervises."""
  (directory / OUTPUT_DIRECTORY).mkdir()
  with (
    (directory / STDOUT_FILE).open('wb') as out,
    (directory / STDERR_FILE).open('wb') as error,
  ):
    process = await asyncio.create_subprocess_exec(
      *job.command,
      cwd=directory,
      env=job.env,
      stdout=out,
      stderr=error,
      start_new_session=True,
    )
  return _CommandHandle(process)


def record_status(directory: Path, status: dict[str, Any]) -> None:
  """record how the run ended, so the collected directory says it on its own.
  The exit code cannot: a job killed at its deadline dies on a signal like any
  other crash."""
  (directory / STATUS_FILE).write_text(json.dumps(status, sort_keys=True) + '\n')
