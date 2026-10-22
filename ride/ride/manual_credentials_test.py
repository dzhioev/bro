import threading
from pathlib import Path
from typing import Any

import ride.manual_credentials as manual_credentials
from ride.credential_applier import CredentialApplier


class BlockingClient:
  """a broker client whose work poll stays unanswered until the client is closed."""

  def __init__(self, calls: list[str], polling: threading.Event):
    self.calls = calls
    self.polling = polling
    self.closed = threading.Event()

  def call(self, kind: str, args: dict[str, Any], timeout: float) -> Any:
    self.calls.append(kind)
    if kind == 'credentials.work':
      self.polling.set()
      if not self.closed.wait(5):
        self.calls.append('unaborted')
        raise TimeoutError('work poll was never aborted')
      raise ConnectionError('broker channel closed awaiting reply')
    return type('Reply', (), {'payload': {'outcome': 'ok'}})()

  def close(self) -> None:
    self.closed.set()

  def __enter__(self) -> 'BlockingClient':
    return self

  def __exit__(self, *exception: object) -> None:
    self.close()


def test_leaving_the_launcher_aborts_the_unanswered_work_poll(tmp_path: Path, monkeypatch):
  calls: list[str] = []
  polling = threading.Event()
  monkeypatch.setattr(
    manual_credentials,
    'register_applier',
    lambda root, mission: {
      'address': 'tcp://token@127.0.0.1:1',
      'lifeline': str(tmp_path / 'life'),
    },
  )
  monkeypatch.setattr(manual_credentials, 'connect', lambda address: address)
  monkeypatch.setattr(manual_credentials, 'Client', lambda address: BlockingClient(calls, polling))

  with manual_credentials.connected_applier('root', 'mission', CredentialApplier({}), tmp_path):
    assert polling.wait(10)

  assert 'unaborted' not in calls
  assert calls == ['credentials.attach', 'credentials.work', 'credentials.close']
