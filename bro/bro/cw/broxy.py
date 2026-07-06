"""host-mode broxy lifecycle: the session-owned `broxy serve` the runner starts.

A container session gets its broxy from the container entrypoint; a host
session gets it here — started by the in-place runner next to the session-local
MCP server, with `BROKER_CHANNEL` rewritten to the local socket before the MCP
server and claude launch. Best-effort by design (the `_broker_enabled` spirit:
the gate must never break a launch): when the broxy cannot start, the caller
keeps the direct upstream channel and only warns.

No broker import: the daemon is the `broxy` console script (broker/broxy.py),
and readiness is a plain socket probe — so this module stays importable in an
environment whose venv cannot import broker.
"""

import socket
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import log, spawn

_READY_TIMEOUT = 10.0


@dataclass
class _SessionBroxy:
  """a session-local `broxy serve` owned by the launching session."""

  process: subprocess.Popen
  address: str  # the rewritten BROKER_CHANNEL value (unix:<local socket>)
  log_path: Path

  def stop(self) -> None:
    self.process.terminate()
    try:
      self.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
      self.process.kill()
      self.process.wait()


def _accepting(socket_path: Path) -> bool:
  probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  try:
    probe.connect(str(socket_path))
    return True
  except OSError:
    return False
  finally:
    probe.close()


def _start_session_broxy(upstream: str, env: Mapping[str, str]) -> Optional[_SessionBroxy]:
  """start `broxy serve` on a session-tempdir socket and wait until it accepts.

  the socket lives outside the workspace tree (a socket in the workspace would
  dirty `git status` and the clean checks). returns None — with a warning — when
  the daemon cannot start or is not accepting within the deadline; the caller
  then keeps the direct upstream channel.
  """
  state = Path(tempfile.mkdtemp(prefix='cw-broxy-'))
  socket_path = state / 'broxy.sock'
  log_path = state / 'broxy.log'
  try:
    with open(log_path, 'w') as log_file:
      process = spawn.popen(
        ['broxy', 'serve', str(socket_path), '--upstream', upstream],
        env=dict(env),
        stdout=log_file,
        stderr=subprocess.STDOUT,
      )
  except OSError as e:
    log.warning('cannot start broxy (%s); keeping the direct broker channel', e)
    return None
  deadline = time.monotonic() + _READY_TIMEOUT
  while True:
    if process.poll() is not None:
      log.warning(
        'broxy exited with code %s during startup (log: %s); keeping the direct broker channel',
        process.returncode,
        log_path,
      )
      return None
    if _accepting(socket_path):
      return _SessionBroxy(process, f'unix:{socket_path}', log_path)
    if time.monotonic() >= deadline:
      process.terminate()
      log.warning(
        'broxy not accepting within %.0fs (log: %s); keeping the direct broker channel',
        _READY_TIMEOUT,
        log_path,
      )
      return None
    time.sleep(0.05)
