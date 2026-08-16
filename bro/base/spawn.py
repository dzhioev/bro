"""subprocess wrappers that spawn children unable to block on interactive input.

Every child runs in a fresh session (`start_new_session=True`), detaching it from
any controlling terminal so a `/dev/tty` open fails with ENXIO instead of blocking.
stdin defaults to /dev/null.

`run` and `run_async` additionally reap the child's whole process *group* on timeout
(or any other error mid-run), not just the direct child. `start_new_session=True`
makes the child a process-group leader, so a SIGKILL to the group also takes out any
grandchildren the child spawned (shell pipelines, backgrounded helpers). Without this,
a timed-out `bash -c 'grep -R ... | sed ...'` would kill only the shell and leave a
`grep` blocked on a FIFO running forever — `subprocess.run`'s own timeout cleanup
signals only the direct child.

`format_result` is the shared shape a finished child takes as agent-tool output.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import AsyncGenerator, Callable
from typing import Literal, Optional

from bro.base.text_window import apply_limit


def _signal_group(pid: int, signal_number: int, fallback: Callable[[], None]) -> None:
  try:
    os.killpg(pid, signal_number)
  except (ProcessLookupError, PermissionError):
    # group already gone, or the leader exited and its pgid was recycled — fall
    # back to signalling just the direct child (a no-op if already reaped).
    fallback()


def kill_group(process: subprocess.Popen | asyncio.subprocess.Process) -> None:
  """SIGKILL the child's whole process group. The child is a process-group leader
  (`start_new_session=True`), so this also reaps grandchildren. `run` calls it on
  timeout; streaming callers that drive their own read loop (e.g. infra's deploy
  runner with a watchdog timer) call it directly."""
  _signal_group(process.pid, signal.SIGKILL, process.kill)


def terminate_group(process: subprocess.Popen | asyncio.subprocess.Process) -> None:
  """SIGTERM the child's whole process group — the graceful sibling of `kill_group`,
  for callers that give the child a chance to clean up and escalate themselves."""
  _signal_group(process.pid, signal.SIGTERM, process.terminate)


def run(
  command,
  *,
  input=None,
  capture_output: bool = False,
  timeout: Optional[float] = None,
  check: bool = False,
  **kwargs,
) -> subprocess.CompletedProcess:
  # `input` and `stdin` are mutually exclusive; only default stdin to /dev/null
  # when the caller hasn't supplied input to feed in.
  if input is not None:
    if kwargs.get('stdin') is not None:
      raise ValueError('stdin and input arguments may not both be used')
    kwargs['stdin'] = subprocess.PIPE
  else:
    kwargs.setdefault('stdin', subprocess.DEVNULL)
  if capture_output:
    if kwargs.get('stdout') is not None or kwargs.get('stderr') is not None:
      raise ValueError('stdout and stderr arguments may not be used with capture_output')
    kwargs['stdout'] = subprocess.PIPE
    kwargs['stderr'] = subprocess.PIPE
  kwargs['start_new_session'] = True

  with subprocess.Popen(command, **kwargs) as process:
    try:
      stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
      # kill the whole group, then drain — once every writer is dead the pipes hit
      # EOF and the second communicate returns instead of hanging on a grandchild
      # that still holds the captured pipe open.
      kill_group(process)
      process.communicate()
      raise
    except BaseException:
      kill_group(process)
      process.wait()
      raise
    return_code = process.poll()
  assert return_code is not None  # communicate returned, so the child has exited
  if check and return_code != 0:
    raise subprocess.CalledProcessError(return_code, process.args, output=stdout, stderr=stderr)
  return subprocess.CompletedProcess(process.args, return_code, stdout, stderr)


@contextlib.asynccontextmanager
async def _reaped(process: asyncio.subprocess.Process) -> AsyncGenerator[None]:
  # whatever ends the wait short of the child's own exit — a timeout, a
  # cancelled await — takes the whole group with it, so no grandchild outlives
  # the call that started it. the exit wait is shielded: the cancellation that
  # brought us here would otherwise abort the reap as well.
  try:
    yield
  except BaseException:
    if process.returncode is None:
      kill_group(process)
      await asyncio.shield(process.wait())
    raise


async def run_async(
  command, *, timeout: Optional[float] = None
) -> subprocess.CompletedProcess[str]:
  """`run`'s awaitable counterpart for the tool path: same detached child and
  process-group reaping, cancellable. stdout and stderr are always captured as
  text. Raises `subprocess.TimeoutExpired` on expiry, like `run`."""
  process = await asyncio.create_subprocess_exec(
    *command,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
  )
  async with _reaped(process):
    try:
      stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as error:
      raise subprocess.TimeoutExpired(command, timeout if timeout is not None else 0) from error
  assert process.returncode is not None  # communicate returned, so the child has exited
  return subprocess.CompletedProcess(
    command, process.returncode, stdout.decode(errors='replace'), stderr.decode(errors='replace')
  )


def format_result(
  process: subprocess.CompletedProcess[str],
  *,
  limit: int,
  keep: Literal['head', 'tail'] = 'head',
) -> str:
  """a finished child's result as agent-tool output: the exit code, then the
  captured output with stderr under a divider, capped to `limit` lines."""
  combined = process.stdout
  if len(process.stderr) > 0:
    combined = (
      f'{combined}\n--- stderr ---\n{process.stderr}' if len(combined) > 0 else process.stderr
    )
  capped = apply_limit(combined, limit, keep=keep)
  if len(capped) == 0:
    return f'exit_code: {process.returncode}'
  return f'exit_code: {process.returncode}\n{capped}'


def popen(command, **kwargs) -> subprocess.Popen:
  kwargs.setdefault('stdin', subprocess.DEVNULL)
  kwargs['start_new_session'] = True
  return subprocess.Popen(command, **kwargs)
