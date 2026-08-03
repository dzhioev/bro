import asyncio
import contextlib
import json
from dataclasses import dataclass

import pytest

import bro.broker.cli as broker_cli
from bro.broker.brotocol import Message
from bro.broker.client import CHANNEL_ENV
from bro.broker.transport import ChannelID
from bro.broker.transports.unix import UnixServerTransport

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
async def running_server(socket_dir):
  transport = UnixServerTransport(str(socket_dir))
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


def test_inert_when_channel_unset(monkeypatch, capsys):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert broker_cli.main(['broker', 'send', 'ping']) == 0
  assert broker_cli.main(['broker', 'request', 'ping']) == 0
  assert broker_cli.main(['broker', 'receive', '--timeout', '0.2']) == 0
  assert capsys.readouterr().out == ''  # stdout stays data-only


def test_payload_must_be_a_json_object(monkeypatch):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  with pytest.raises(SystemExit):
    broker_cli.main(['broker', 'send', 'ping', 'not-json'])
  with pytest.raises(SystemExit):
    broker_cli.main(['broker', 'send', 'ping', '[1, 2]'])


@pytest.mark.asyncio
async def test_send_reaches_the_host(socket_dir, monkeypatch):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
    argv = ['broker', 'send', 'started', '{"trail_id": "t1"}']
    assert await asyncio.to_thread(broker_cli.main, argv) == 0

    channel, message = await _next(server.sink.messages)
    assert channel == provisioned.channel
    assert message.type == 'started'
    assert message.payload == {'trail_id': 't1'}


@pytest.mark.asyncio
async def test_request_prints_the_correlated_reply(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
    argv = ['broker', 'request', 'ping', '--timeout', str(TIMEOUT)]
    main_task = asyncio.create_task(asyncio.to_thread(broker_cli.main, argv))

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='reply', payload={'pong': True}, in_reply_to=request_message.id)
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['type'] == 'reply'
    assert printed['in_reply_to'] == request_message.id
    assert printed['payload'] == {'pong': True}


@pytest.mark.asyncio
async def test_request_timeout_exits_nonzero(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
    argv = ['broker', 'request', 'ping', '--timeout', '0.2']
    assert await asyncio.to_thread(broker_cli.main, argv) == 1
    assert capsys.readouterr().out == ''


@pytest.mark.asyncio
async def test_receive_prints_one_message(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
    argv = ['broker', 'receive', '--timeout', str(TIMEOUT)]
    main_task = asyncio.create_task(asyncio.to_thread(broker_cli.main, argv))

    await _next(server.sink.connects)  # the CLI's client attached
    await server.transport.send(provisioned.channel, Message(type='status', payload={'n': 7}))

    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['type'] == 'status'
    assert printed['payload'] == {'n': 7}


@pytest.mark.asyncio
async def test_receive_nothing_exits_nonzero(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
    argv = ['broker', 'receive', '--timeout', '0.2']
    assert await asyncio.to_thread(broker_cli.main, argv) == 1
    assert capsys.readouterr().out == ''
