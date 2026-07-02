import asyncio
import contextlib
from dataclasses import dataclass

import pytest

from broker.brotocol import Message
from broker.client import CHANNEL_ENV, Client
from broker.transport import ChannelID
from broker.transports.unix import UnixClientTransport, UnixServerTransport

TIMEOUT = 5.0


class StubSink:
  """records inbound traffic onto asyncio queues the test coroutine can await."""

  def __init__(self):
    self.connects: asyncio.Queue = asyncio.Queue()  # channel
    self.messages: asyncio.Queue = asyncio.Queue()  # (channel, message)
    self.disconnects: asyncio.Queue = asyncio.Queue()  # channel

  async def on_connect(self, channel: ChannelID) -> None:
    self.connects.put_nowait(channel)

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    self.messages.put_nowait((channel, message))

  async def on_disconnect(self, channel: ChannelID) -> None:
    self.disconnects.put_nowait(channel)


@dataclass
class Harness:
  transport: UnixServerTransport
  sink: StubSink


@contextlib.asynccontextmanager
async def running_server(tmp_path):
  transport = UnixServerTransport(str(tmp_path / 'broker'))
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


def test_from_env_returns_none_when_unset(monkeypatch):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert Client.from_env() is None


@pytest.mark.asyncio
async def test_from_env_connects_and_sends(tmp_path, monkeypatch):
  async with running_server(tmp_path) as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
    client = Client.from_env()
    assert client is not None

    await asyncio.to_thread(client.send, 'started', {'trail_id': 't1'})
    channel, message = await _next(server.sink.messages)
    assert channel == provisioned.channel
    assert message.type == 'started'
    assert message.payload == {'trail_id': 't1'}
    assert message.in_reply_to is None
    client.close()


@pytest.mark.asyncio
async def test_request_correlates_and_sets_unrelated_aside(tmp_path):
  async with running_server(tmp_path) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {'n': 1}, TIMEOUT))

    channel, request_message = await _next(server.sink.messages)
    assert request_message.type == 'ping'
    unrelated = Message(type='status', payload={'note': 'unrelated'})
    await server.transport.send(channel, unrelated)
    await server.transport.send(
      channel, Message(type='reply', payload={'pong': 1}, in_reply_to=request_message.id)
    )

    reply = await asyncio.wait_for(request_task, TIMEOUT)
    assert reply.type == 'reply'
    assert reply.in_reply_to == request_message.id
    assert reply.payload == {'pong': 1}

    # the unrelated message request() read past was set aside, not dropped
    set_aside = await asyncio.to_thread(client.receive, 0.2)
    assert set_aside is not None
    assert set_aside.id == unrelated.id
    client.close()


@pytest.mark.asyncio
async def test_request_times_out_without_reply(tmp_path):
  async with running_server(tmp_path) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    with pytest.raises(TimeoutError):
      await asyncio.to_thread(client.request, 'ping', {}, 0.2)
    client.close()


@pytest.mark.asyncio
async def test_request_raises_on_channel_close(tmp_path):
  async with running_server(tmp_path) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    await _next(server.sink.messages)  # the request reached the host

    await server.transport.close(provisioned.channel)
    with pytest.raises(ConnectionError):
      await asyncio.wait_for(request_task, TIMEOUT)
    client.close()


@pytest.mark.asyncio
async def test_receive_returns_none_on_timeout(tmp_path):
  async with running_server(tmp_path) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    assert await asyncio.to_thread(client.receive, 0.2) is None
    client.close()
