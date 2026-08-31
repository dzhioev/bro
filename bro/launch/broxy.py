"""host-mode session broxy launch through the `broxy launch` console command.

The broker package stays a lazy dependency: this module invokes the console script
instead of importing the broxy implementation. `session_broxy` consumes
`BROKER_UPSTREAM` and publishes `BROKER_CHANNEL` only after readiness;
a failed launch leaves the former set so clients report it explicitly.
"""

import contextlib
import os
import signal
import subprocess
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import log, spawn
from bro.launch.broker_environment import CHANNEL_ENV, UPSTREAM_ENV, broxy_log_path

_LAUNCH_TIMEOUT = 10.0
START_SESSION_BROXY_ENV = 'BRO_START_SESSION_BROXY'


@dataclass
class _SessionBroxy:
  """a session-local broxy daemon owned by the launching session."""

  pid: int
  address: str
  log_path: Path

  def stop(self) -> None:
    try:
      os.kill(self.pid, signal.SIGTERM)
    except ProcessLookupError:
      pass


def _start_session_broxy(upstream: str, env: Mapping[str, str]) -> Optional[_SessionBroxy]:
  """launch a broxy on a loopback port of its own, returning None on failure."""
  log_path = broxy_log_path(env)
  log_path.parent.mkdir(parents=True, exist_ok=True)
  command = [
    'broxy',
    'launch',
    '--upstream',
    upstream,
    '--log-file',
    str(log_path),
    '--timeout',
    str(_LAUNCH_TIMEOUT),
  ]
  try:
    with open(log_path, 'a') as log_file:
      result = spawn.run(
        command,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=log_file,
        text=True,
        timeout=_LAUNCH_TIMEOUT + 10,
      )
  except (OSError, subprocess.TimeoutExpired) as error:
    log.warning('cannot launch broxy (%s); the session gets no broker channel', error)
    return None
  if result.returncode != 0:
    log.warning('broxy launch failed (log: %s); the session gets no broker channel', log_path)
    return None

  fields = result.stdout.rstrip('\n').split('\t')
  if len(fields) != 2:
    log.warning('broxy launch returned invalid output; the session gets no broker channel')
    return None
  address, pid_text = fields
  try:
    pid = int(pid_text)
  except ValueError:
    log.warning('broxy launch returned an invalid pid; the session gets no broker channel')
    return None
  log.verbose('session broxy listening (pid %d)', pid)  # the address carries its token
  return _SessionBroxy(pid, address, log_path)


@contextlib.contextmanager
def session_broxy() -> Generator[None]:
  """Give a marked host session its local client channel."""
  requested = os.environ.pop(START_SESSION_BROXY_ENV, None)
  upstream = os.environ.get(UPSTREAM_ENV)
  if requested is None or upstream is None:
    yield
    return

  previous_channel = os.environ.pop(CHANNEL_ENV, None)
  broxy = _start_session_broxy(upstream, os.environ)
  if broxy is not None:
    os.environ.pop(UPSTREAM_ENV)
    os.environ[CHANNEL_ENV] = broxy.address
  try:
    yield
  finally:
    if broxy is not None:
      broxy.stop()
      os.environ.pop(CHANNEL_ENV, None)
      os.environ[UPSTREAM_ENV] = upstream
    if previous_channel is not None:
      os.environ[CHANNEL_ENV] = previous_channel
