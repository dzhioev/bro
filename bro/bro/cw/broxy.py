"""host-mode broxy launch: the session-owned `broxy serve` the runner starts.

Launching a broxy is the same thin sequence everywhere — start `broxy serve`,
gate on `broxy await`, rewrite `BROKER_CHANNEL` to the local socket; serve
fails loudly rather than restarting (broker/broxy.py owns that policy). A
container session gets the sequence from the container entrypoint (socket and
log under /tmp); a host session gets it here — run by the in-place runner next
to the session-local MCP server, on a session-tempdir socket, with the daemon
stopped on session exit.

A set `BROKER_CHANNEL` always names a broxy socket: when the broxy cannot run
— missing from the venv (a workspace based on a pre-broxy ref) or not ready
within the gate — the caller unsets the channel and the session runs without
one; the launch itself still proceeds (the gate must never break a launch).

No broker import: the daemon and the readiness gate are the `broxy` console
script (broker/broxy.py), so this module stays importable in an environment
whose venv cannot import broker — the very case the no-channel degrade covers.
"""

import subprocess
import tempfile
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


def _start_session_broxy(upstream: str, env: Mapping[str, str]) -> Optional[_SessionBroxy]:
  """start `broxy serve` on a session-tempdir socket and gate on `broxy await`.

  the socket — with its log next to it — lives outside the workspace tree (a
  socket in the workspace would dirty `git status` and the clean checks).
  returns None — with a warning — when the daemon cannot start or is not ready
  within the gate; the caller then unsets BROKER_CHANNEL.
  """
  state = Path(tempfile.mkdtemp(prefix='cw-broxy-'))
  socket_path = state / 'broxy.sock'
  log_path = state / 'broxy.log'
  try:
    with open(log_path, 'a') as log_file:
      process = spawn.popen(
        ['broxy', 'serve', str(socket_path), '--upstream', upstream],
        env=dict(env),
        stdout=log_file,
        stderr=subprocess.STDOUT,
      )
  except OSError as e:
    log.warning('cannot start broxy (%s); the session gets no broker channel', e)
    return None
  broxy = _SessionBroxy(process, f'unix:{socket_path}', log_path)
  try:
    with open(log_path, 'a') as log_file:
      ready = spawn.run(
        ['broxy', 'await', str(socket_path), '--timeout', str(_READY_TIMEOUT)],
        env=dict(env),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        timeout=_READY_TIMEOUT + 10,
      )
  except (OSError, subprocess.TimeoutExpired) as e:
    broxy.stop()
    log.warning('broxy readiness gate failed (%s); the session gets no broker channel', e)
    return None
  if ready.returncode == 0:
    return broxy
  broxy.stop()
  log.warning(
    'broxy not ready within %.0fs (log: %s); the session gets no broker channel',
    _READY_TIMEOUT,
    log_path,
  )
  return None
