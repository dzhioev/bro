"""subprocess wrappers that spawn children unable to block on interactive input.

Every child runs in a fresh session (`start_new_session=True`), detaching it from
any controlling terminal so a `/dev/tty` open fails with ENXIO instead of blocking.
stdin defaults to /dev/null.

`run` and `run_async` additionally reap the child's whole process *group* on timeout
(or any other error mid-run), not just the direct child;
`run` gives the group a SIGTERM first when the caller names a `grace`. `start_new_session=True`
makes the child a process-group leader, so a SIGKILL to the group also takes out any
grandchildren the child spawned (shell pipelines, backgrounded helpers). Without this,
a timed-out `bash -c 'grep -R ... | sed ...'` would kill only the shell and leave a
`grep` blocked on a FIFO running forever — `subprocess.run`'s own timeout cleanup
signals only the direct child.

`format_result` is the shared shape a finished child takes as agent-tool output.

`console_script` names a child by path instead of by bare name, for the machinery
a process spawns beside itself.
"""

import asyncio
import contextlib
import errno
import os
import signal
import subprocess
import sys
import sysconfig
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Optional

from bro.base.text_window import window


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


def _end_group(process: subprocess.Popen, grace: Optional[float]) -> None:
  """end the child's group past its deadline: SIGKILL outright, or with `grace`
  a SIGTERM first and the SIGKILL once those seconds pass without an exit
  — or sooner, whatever else ends the wait."""
  try:
    if grace is not None:
      terminate_group(process)
      with contextlib.suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=grace)
  finally:
    kill_group(process)


def run(
  command,
  *,
  input=None,
  capture_output: bool = False,
  timeout: Optional[float] = None,
  grace: Optional[float] = None,
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
      # end the whole group, then drain — once every writer is dead the pipes hit
      # EOF and the last communicate returns instead of hanging on a grandchild
      # that still holds the captured pipe open.
      _end_group(process, grace)
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


def format_result(process: subprocess.CompletedProcess[str], *, offset: int, limit: int) -> str:
  """a finished child's result as agent-tool output: the exit code, then a window
  of `limit` lines from line `offset` of the captured output, stderr under a
  divider."""
  combined = process.stdout
  if len(process.stderr) > 0:
    combined = (
      f'{combined}\n--- stderr ---\n{process.stderr}' if len(combined) > 0 else process.stderr
    )
  windowed = window(combined, offset, limit)
  if len(windowed) == 0:
    return f'exit_code: {process.returncode}'
  return f'exit_code: {process.returncode}\n{windowed}'


def popen(command, **kwargs) -> subprocess.Popen:
  kwargs.setdefault('stdin', subprocess.DEVNULL)
  kwargs['start_new_session'] = True
  return subprocess.Popen(command, **kwargs)


def console_script(name: str) -> str:
  """the absolute path of console script `name` in the running interpreter's
  environment. Machinery a process spawns beside itself resolves this way rather
  than by bare name: the PATH it was launched with is not required to carry it."""
  path = Path(sysconfig.get_path('scripts')) / name
  if not path.is_file():
    raise FileNotFoundError(
      errno.ENOENT, f'no console script beside the running {sys.executable}', str(path)
    )
  return str(path)
