"""Supervisor-owned access to one running session's private credential paths."""

import contextlib
import json
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bro.base import spawn
from bro.base.credential_apply import atomic_file
from bro.base.credential_environment import INSTALL_DIRECTORY_ENV

APPLY_TIMEOUT = 15


class ApplierClosed(RuntimeError):
  pass


class CredentialApplier:
  def __init__(self, environment: Mapping[str, str], container: str | None = None):
    self.environment = dict(environment)
    self.container = container
    self._lock = threading.Lock()
    self._closed = threading.Event()

  def close(self) -> None:
    self._closed.set()
    with self._lock:
      pass

  def apply(self, request: dict[str, Any], receipt: Path) -> int:
    with self._lock:
      if self._closed.is_set():
        raise ApplierClosed('credential session has ended')
      return self._apply(request, receipt)

  def _apply(self, request: dict[str, Any], receipt: Path) -> int:
    environment = self.environment
    store = environment['BRO_STORE']
    directory = environment[INSTALL_DIRECTORY_ENV]
    with tempfile.TemporaryDirectory(prefix='ride-credential-apply-') as temporary:
      payload = Path(temporary) / 'request.json'
      atomic_file(payload, json.dumps(request).encode())
      if self.container is None:
        command = [spawn.console_script('credentials'), 'apply', directory, str(payload)]
        result = spawn.run(
          command, env=environment, capture_output=True, text=True, timeout=APPLY_TIMEOUT
        )
      else:
        destination = store + '/.override-request.json'
        with contextlib.ExitStack() as cleanup:
          spawn.run(
            ['docker', 'cp', str(payload), f'{self.container}:{destination}'],
            check=True,
            capture_output=True,
            timeout=APPLY_TIMEOUT,
          )
          cleanup.callback(
            subprocess.run,
            ['docker', 'exec', self.container, 'rm', '-f', destination],
            capture_output=True,
            timeout=APPLY_TIMEOUT,
          )
          spawn.run(
            ['docker', 'exec', self.container, 'chown', 'ride:ride', destination],
            check=True,
            capture_output=True,
            timeout=APPLY_TIMEOUT,
          )
          command = [
            'docker',
            'exec',
            '--user',
            'ride',
            '--env',
            'BRO_STORE=' + store,
            '--env',
            'PATH=' + environment['PATH'],
            self.container,
            'credentials',
            'apply',
            directory,
            destination,
          ]
          result = spawn.run(command, capture_output=True, text=True, timeout=APPLY_TIMEOUT)
      if result.returncode != 0:
        raise RuntimeError(f'credential apply failed: {result.stderr.strip()}')
      generation = json.loads(result.stdout)['generation']
      atomic_file(
        receipt, json.dumps({'operation': request['operation'], 'generation': generation}).encode()
      )
      return generation
