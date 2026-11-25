"""Lifetimes around a session's run: its recorder."""

import contextlib
import os
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

from bro.base import log, spawn
from bro.native.run_log import private_directory

RECORDER_COMMAND = 'bro.native.trail-recorder'
_STOP_TIMEOUT = 60.0


def _stop(process: subprocess.Popen, log_path: Path) -> None:
  process.terminate()
  try:
    process.wait(timeout=_STOP_TIMEOUT)
  except subprocess.TimeoutExpired:
    log.warning(
      'session recorder did not finish within %.0fs; killing (log: %s)', _STOP_TIMEOUT, log_path
    )
    process.kill()
    process.wait()


@contextlib.contextmanager
def session_recorder(
  run_log: Path, log_path: Path, env: Mapping[str, str]
) -> Iterator[subprocess.Popen]:
  """Start detached, wait for readiness, and stop after the session run exits."""
  private_directory(log_path.parent)
  private_directory(run_log.parent)
  with contextlib.ExitStack() as lifetime:
    read_fd, write_fd = os.pipe()
    reader = lifetime.enter_context(os.fdopen(read_fd, 'rb'))
    try:
      with os.fdopen(write_fd, 'wb') as ready, log_path.open('ab') as output:
        process = spawn.popen(
          [
            spawn.console_script(RECORDER_COMMAND),
            '--run-log',
            str(run_log),
            '--log-path',
            str(log_path),
            '--ready-fd',
            str(ready.fileno()),
          ],
          env=dict(env),
          stdout=output,
          stderr=subprocess.STDOUT,
          pass_fds=(ready.fileno(),),
        )
        lifetime.callback(_stop, process, log_path)
      if reader.read(1) != b'1':
        raise RuntimeError(f'session recorder exited before readiness (log: {log_path})')
    except OSError as exception:
      raise RuntimeError(
        f'cannot start session recorder: {exception} (log: {log_path})'
      ) from exception
    yield process
