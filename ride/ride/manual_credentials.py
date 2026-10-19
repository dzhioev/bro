"""A manual launcher's retained operator connection to its child's applier."""

import contextlib
import json
import subprocess
import threading
from collections.abc import Generator
from pathlib import Path

from bro.base import log
from bro.broker.client import Client
from bro.broker.transport import connect
from ride.credential_applier import CredentialApplier
from ride.credential_overrides import LIFETIME_READY, receipt_path
from ride.operator import register_applier


@contextlib.contextmanager
def connected_applier(
  root: str,
  mission: str,
  applier: CredentialApplier,
  records: Path,
) -> Generator[None]:
  registration = register_applier(root, mission)
  address = registration['address']
  stopped = threading.Event()

  def serve() -> None:
    while not stopped.is_set():
      try:
        with Client(connect(address)) as client:
          while not stopped.is_set():
            response = client.call('credentials.work', {}, timeout=15).payload
            if response.get('outcome') != 'ok':
              raise RuntimeError(f'manual credential applier refused: {response}')
            work = response.get('value')
            if work is None:
              continue
            request = json.loads(Path(work['request_file']).read_text())
            try:
              applier.apply(request, receipt_path(records, request['operation']))
            except (
              OSError,
              RuntimeError,
              TimeoutError,
              ValueError,
              subprocess.SubprocessError,
            ) as error:
              log.warning(
                'manual credential operation %s awaiting retry: %s', request['operation'], error
              )
              stopped.wait(0.2)
            else:
              client.call(
                'credentials.acknowledge', {'operation': request['operation']}, timeout=15
              )
      except (ConnectionError, OSError, TimeoutError) as error:
        if not stopped.is_set():
          log.warning('manual credential applier reconnecting: %s', error)
          stopped.wait(1)
    try:
      with Client(connect(address)) as client:
        client.call('credentials.close', {}, timeout=15)
    except (ConnectionError, OSError, TimeoutError) as error:
      log.verbose('manual credential applier host has ended: %s', error)

  with contextlib.ExitStack() as setup:
    lifeline = setup.enter_context(Path(registration['lifeline']).open('wb', buffering=0))
    lifeline.write(LIFETIME_READY)
    with Client(connect(address)) as client:
      response = client.call('credentials.attach', {}, timeout=15).payload
      if response.get('outcome') != 'ok':
        raise RuntimeError(f'manual credential applier attach refused: {response}')

    def run() -> None:
      with contextlib.closing(lifeline), contextlib.closing(applier):
        serve()

    thread = threading.Thread(target=run, name='manual-credential-applier', daemon=True)
    thread.start()
    setup.pop_all()
  try:
    yield
  finally:
    stopped.set()
    applier.close()
    thread.join()
