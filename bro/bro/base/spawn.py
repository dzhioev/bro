"""subprocess wrappers that spawn children unable to block on interactive input.

Every child runs in a fresh session (`start_new_session=True`), detaching it from
any controlling terminal so a `/dev/tty` open fails with ENXIO instead of blocking.
stdin defaults to /dev/null.

`run` additionally reaps the child's whole process *group* on timeout (or any other
error mid-run), not just the direct child. `start_new_session=True` makes the child a
process-group leader, so a SIGKILL to the group also takes out any grandchildren the
child spawned (shell pipelines, backgrounded helpers). Without this, a timed-out
`bash -c 'grep -R ... | sed ...'` would kill only the shell and leave a `grep`
blocked on a FIFO running forever — `subprocess.run`'s own timeout cleanup signals
only the direct child.
"""

import os
import signal
import subprocess
from typing import Optional


def kill_group(process: subprocess.Popen) -> None:
  """SIGKILL the child's whole process group. The child is a process-group leader
  (`start_new_session=True`), so this also reaps grandchildren. `run` calls it on
  timeout; streaming callers that drive their own read loop (e.g. infra's deploy
  runner with a watchdog timer) call it directly."""
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except (ProcessLookupError, PermissionError):
    # group already gone, or the leader exited and its pgid was recycled — fall
    # back to signalling just the direct child (a no-op if already reaped).
    process.kill()


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
    retcode = process.poll()
  assert retcode is not None  # communicate returned, so the child has exited
  if check and retcode != 0:
    raise subprocess.CalledProcessError(retcode, process.args, output=stdout, stderr=stderr)
  return subprocess.CompletedProcess(process.args, retcode, stdout, stderr)


def popen(command, **kwargs) -> subprocess.Popen:
  kwargs.setdefault('stdin', subprocess.DEVNULL)
  kwargs['start_new_session'] = True
  return subprocess.Popen(command, **kwargs)
