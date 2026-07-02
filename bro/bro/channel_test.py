import sys
from typing import Optional

import pytest

from bro.bro import BaseBro, BroRaised
from bro.channel import BroChannel
from broker.brotocol import Message
from broker.client import CHANNEL_ENV, Client
from broker.transport import ClientTransport
from llm.llm import LLM
from llm.observer import NullObserver, Observer
from llm.tracker import NullTracker


class FakeClientTransport(ClientTransport):
  def __init__(self):
    self.sent: list[Message] = []
    self.closed = False

  def send(self, message: Message) -> None:
    self.sent.append(message)

  def receive(self, timeout: Optional[float]) -> Optional[Message]:
    return None

  def close(self) -> None:
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
    # poisoned too — a cached broker.client would satisfy the from-import on its own
    monkeypatch.setitem(sys.modules, 'broker', None)
    monkeypatch.setitem(sys.modules, 'broker.client', None)
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
    channel.completed('the answer', 'terminal')
    assert len(transport.sent) == 1
    message = transport.sent[0]
    assert message.type == 'completed'
    assert message.payload == {'result': 'the answer', 'end_reason': 'terminal'}

  def test_close_closes_client(self):
    channel, transport = _make_channel()
    channel.close()
    assert transport.closed is True


class _StubLLM(LLM):
  def __init__(self, response: str = 'ok', error: Optional[BaseException] = None):
    super().__init__(mcp_servers=None)
    self._response = response
    self._error = error

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    if self._error is not None:
      raise self._error
    return self._response


class _ChannelBro(BaseBro):
  name = 'channel-bro'
  description = 'd'

  def __init__(self, channel: Optional[BroChannel], llm: LLM):
    super().__init__(system_prompt='')
    self._channel = channel
    self._llm_stub = llm

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _make_channel(self) -> Optional[BroChannel]:
    return self._channel

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self._llm_stub


class _TrailIDTracker(NullTracker):
  def start_trail(self, bro, llm_spec, system_prompt, parent, interactive, entry_point) -> str:
    return 'trail-42'


class TestRunLifecycle:
  @pytest.mark.asyncio
  async def test_terminal_run_emits_started_and_completed(self):
    channel, transport = _make_channel()
    bro = _ChannelBro(channel, _StubLLM(response='the result'))
    result = await bro.run('input', tracker=_TrailIDTracker())
    assert result == 'the result'
    assert [m.type for m in transport.sent] == ['started', 'completed']
    assert transport.sent[0].payload == {'trail_id': 'trail-42'}
    assert transport.sent[1].payload == {'result': 'the result', 'end_reason': 'terminal'}
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_raised_run_emits_completed_with_reason(self):
    channel, transport = _make_channel()
    bro = _ChannelBro(channel, _StubLLM(error=BroRaised('cannot fulfill')))
    with pytest.raises(BroRaised):
      await bro.run('input')
    assert [m.type for m in transport.sent] == ['started', 'completed']
    assert transport.sent[1].payload == {'result': 'cannot fulfill', 'end_reason': 'raised'}
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_error_run_emits_completed_with_exception_string(self):
    channel, transport = _make_channel()
    bro = _ChannelBro(channel, _StubLLM(error=RuntimeError('kaboom')))
    with pytest.raises(RuntimeError):
      await bro.run('input')
    assert [m.type for m in transport.sent] == ['started', 'completed']
    assert transport.sent[1].payload == {'result': 'kaboom', 'end_reason': 'error'}
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_started_follows_start_trail_and_completed_follows_end_trail(self):
    events: list[str] = []

    class OrderTracker(NullTracker):
      def start_trail(self, bro, llm_spec, system_prompt, parent, interactive, entry_point) -> str:
        events.append('start_trail')
        return 'tid'

      def end_trail(self, reason) -> None:
        events.append('end_trail')

    class OrderTransport(FakeClientTransport):
      def send(self, message: Message) -> None:
        events.append(message.type)
        super().send(message)

    transport = OrderTransport()
    bro = _ChannelBro(BroChannel(Client(transport)), _StubLLM())
    await bro.run('input', tracker=OrderTracker())
    assert events == ['start_trail', 'started', 'end_trail', 'completed']

  @pytest.mark.asyncio
  async def test_run_without_channel_emits_nothing(self, monkeypatch):
    monkeypatch.delenv(CHANNEL_ENV, raising=False)

    class DefaultChannelBro(BaseBro):
      name = 'default-channel'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _make_observer(self) -> Observer:
        return NullObserver()

      def _create_llm(self, *, interactive: bool) -> LLM:
        return _StubLLM(response='fine')

    assert await DefaultChannelBro().run('input') == 'fine'

  @pytest.mark.asyncio
  async def test_send_does_not_emit_lifecycle(self):
    channel, transport = _make_channel()
    bro = _ChannelBro(channel, _StubLLM(response='chat'))
    assert await bro.send('hi') == 'chat'
    assert transport.sent == []
