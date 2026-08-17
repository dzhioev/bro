"""host-mode session broxy launch through the `broxy launch` console command.

The broker package stays a lazy dependency: this module invokes the console script
instead of importing broker, so a workspace based on a pre-broxy ref can still
start without a channel. The caller owns the fail-open policy and unsets
`BROKER_CHANNEL` when launch fails.
"""

import contextlib
import os
import signal
import subprocess
import tempfile
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import log, spawn

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
  """launch a broxy on a session-tempdir socket, returning None on failure."""
  state = Path(tempfile.mkdtemp(prefix='cw-broxy-'))
  socket_path = state / 'broxy.sock'
  log_path = state / 'broxy.log'
  command = [
    'broxy',
    'launch',
    str(socket_path),
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
  log.verbose('session broxy at %s (pid %d)', address, pid)
  return _SessionBroxy(pid, address, log_path)


@contextlib.contextmanager
def session_broxy() -> Generator[None]:
  """give a broker-root host runner its session-local proxy channel."""
  requested = os.environ.pop(START_SESSION_BROXY_ENV, None)
  upstream = os.environ.get('BROKER_CHANNEL')
  if requested is None or upstream is None:
    yield
    return

  broxy = _start_session_broxy(upstream, os.environ)
  if broxy is None:
    os.environ.pop('BROKER_CHANNEL', None)
  else:
    os.environ['BROKER_CHANNEL'] = broxy.address
  try:
    yield
  finally:
    if broxy is not None:
      broxy.stop()
    os.environ['BROKER_CHANNEL'] = upstream
