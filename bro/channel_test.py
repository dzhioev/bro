import sys
from typing import Optional

from bro.broker.brotocol import Message
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.transport import ClientTransport
from bro.channel import BroChannel


class FakeClientTransport(ClientTransport):
  def __init__(self):
    self.sent: list[Message] = []
    self.closed = False

  def send(self, message: Message) -> None:
    self.sent.append(message)

  def receive(self, timeout: Optional[float]) -> Optional[Message]:
    return None

  def close(self, confirm: bool = False) -> None:
    self.closed = True


def _make_channel() -> tuple[BroChannel, FakeClientTransport]:
  transport = FakeClientTransport()
  return BroChannel(Client(transport)), transport


class TestBroChannel:
  def test_from_env_returns_none_when_unset(self, monkeypatch):
    monkeypatch.delenv(CHANNEL_ENV, raising=False)
    assert BroChannel.from_env() is None

  def test_from_env_returns_none_when_broker_unimportable(self, monkeypatch):
    # an environment provisioned before broker existed: the channel env is set but
    # the package cannot be imported — the hook must stay inert, not crash the run
    monkeypatch.setenv(CHANNEL_ENV, 'unix:/run/broker.sock')
    # None-poisoning makes the import machinery raise ImportError; the submodule must be
    # poisoned too — a cached bro.broker.client would satisfy the from-import on its own
    monkeypatch.setitem(sys.modules, 'bro.broker', None)
    monkeypatch.setitem(sys.modules, 'bro.broker.client', None)
    assert BroChannel.from_env() is None

  def test_started_sends_tagged_message(self):
    channel, transport = _make_channel()
    channel.started('trail-1')
    assert len(transport.sent) == 1
    message = transport.sent[0]
    assert message.type == 'started'
    assert message.payload == {'trail_id': 'trail-1'}
    assert message.in_reply_to is None

  def test_completed_sends_result_and_end_reason(self):
    channel, transport = _make_channel()
    channel.completed('the answer', 'ok')
    assert len(transport.sent) == 1
    message = transport.sent[0]
    assert message.type == 'completed'
    assert message.payload == {'result': 'the answer', 'end_reason': 'ok'}

  def test_close_closes_client(self):
    channel, transport = _make_channel()
    channel.close()
    assert transport.closed is True
