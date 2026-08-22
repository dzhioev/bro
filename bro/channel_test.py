import sys
from typing import Optional

import pytest

from bro.broker.brotocol import Message
from bro.broker.client import CHANNEL_ENV, EXCHANGE_ENV, Client
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
  return BroChannel(Client(transport), 'X'), transport


class TestBroChannel:
  def test_from_env_returns_none_when_unset(self, monkeypatch):
    monkeypatch.delenv(CHANNEL_ENV, raising=False)
    assert BroChannel.from_env() is None

  def test_from_env_raises_when_the_exchange_is_missing(self, monkeypatch):
    # a channel without the exchange id is a mis-provisioned launch: the run could
    # not correlate its lifecycle, so it must fail loudly rather than emit garbage
    transport = FakeClientTransport()
    monkeypatch.setattr('bro.broker.client.connect', lambda address: transport)
    monkeypatch.setenv(CHANNEL_ENV, 'tcp://token@127.0.0.1:9')
    monkeypatch.delenv(EXCHANGE_ENV, raising=False)
    with pytest.raises(ValueError, match=EXCHANGE_ENV):
      BroChannel.from_env()
    assert transport.closed  # the channel it opened before noticing is released

  def test_from_env_returns_none_when_broker_unimportable(self, monkeypatch):
    # an environment provisioned before broker existed: the channel env is set but
    # the package cannot be imported — the hook must stay inert, not crash the run
    monkeypatch.setenv(CHANNEL_ENV, 'tcp://token@127.0.0.1:9')
    # None-poisoning makes the import machinery raise ImportError; the submodule must be
    # poisoned too — a cached bro.broker.client would satisfy the from-import on its own
    monkeypatch.setitem(sys.modules, 'bro.broker', None)
    monkeypatch.setitem(sys.modules, 'bro.broker.client', None)
    assert BroChannel.from_env() is None

  def test_started_emits_progress_on_the_exchange(self):
    channel, transport = _make_channel()
    channel.started('trail-1')
    assert len(transport.sent) == 1
    message = transport.sent[0]
    assert message.type == 'progress'
    assert message.request == 'X'
    assert message.payload == {'trail_id': 'trail-1'}

  def test_started_carries_the_workspace_when_given(self):
    channel, transport = _make_channel()
    channel.started('trail-1', workspace='ws-1')
    assert transport.sent[0].payload == {'trail_id': 'trail-1', 'workspace': 'ws-1'}

  def test_completed_ok_emits_the_ok_result(self):
    channel, transport = _make_channel()
    channel.completed('the answer', 'ok')
    assert len(transport.sent) == 1
    message = transport.sent[0]
    assert message.type == 'result'
    assert message.request == 'X'
    assert message.payload == {'outcome': 'ok', 'value': 'the answer'}

  def test_completed_raised_emits_failed_with_the_reason(self):
    channel, transport = _make_channel()
    channel.completed('cannot: no key', 'raised')
    assert transport.sent[0].payload == {
      'outcome': 'failed',
      'error': 'cannot: no key',
      'detail': {'reason': 'raised'},
    }

  def test_completed_error_without_a_result_omits_the_error_field(self):
    channel, transport = _make_channel()
    channel.completed(None, 'error')
    assert transport.sent[0].payload == {'outcome': 'failed', 'detail': {'reason': 'error'}}

  def test_close_closes_client(self):
    channel, transport = _make_channel()
    channel.close()
    assert transport.closed is True
