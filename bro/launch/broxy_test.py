"""live test of the host-mode `broxy launch` wrapper against a real
provisioned upstream socket, plus the no-channel degrade paths that keep a
launch alive without one."""

import asyncio
import contextlib
import os
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

import bro.launch.broxy as ride_broxy
from bro.broker import brotocol
from bro.broker.brotocol import Message
from bro.broker.client import Client
from bro.broker.transport import ChannelID, connect
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.launch.broxy import _start_session_broxy

TIMEOUT = 10.0
_DEAD_UPSTREAM = 'tcp://upstream-token@127.0.0.1:9'


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
  transport: TcpServerTransport
  sink: StubSink


@contextlib.asynccontextmanager
async def running_server():
  transport = TcpServerTransport([LOCAL_HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


@pytest.mark.asyncio
async def test_session_broxy_serves_the_rewritten_channel():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    broxy = await asyncio.to_thread(
      _start_session_broxy, provisioned.host_endpoint.address(LOCAL_HOST), os.environ
    )
    assert broxy is not None
    try:
      client = Client(await asyncio.to_thread(connect, broxy.address))
      request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
      channel, message = await asyncio.wait_for(server.sink.messages.get(), TIMEOUT)
      assert channel == provisioned.channel  # the proxy rides the session's one channel
      assert message.kind == 'ping'
      await server.transport.send(channel, brotocol.result(message.id, 'ok', value={'pong': {}}))
      reply = await asyncio.wait_for(request_task, TIMEOUT)
      assert reply.type == 'result'
      assert reply.request == message.id
      client.close()
    finally:
      broxy.stop()


def test_start_returns_none_when_the_upstream_is_unreachable(monkeypatch):
  monkeypatch.setattr(ride_broxy, '_LAUNCH_TIMEOUT', 1.0)
  broxy = _start_session_broxy(_DEAD_UPSTREAM, os.environ)
  assert broxy is None


def test_start_returns_none_without_the_console_script(tmp_path):
  # a venv without broxy (a workspace based on a pre-broxy ref): the serve
  # spawn itself fails, and the caller is left to unset BROKER_CHANNEL
  broxy = _start_session_broxy(_DEAD_UPSTREAM, {'PATH': str(tmp_path)})
  assert broxy is None


def test_session_broxy_rewrites_only_a_marked_host_root(monkeypatch):
  daemon = MagicMock(address='tcp://local-token@127.0.0.1:8')
  start = MagicMock(return_value=daemon)
  monkeypatch.setattr(ride_broxy, '_start_session_broxy', start)
  monkeypatch.setenv(ride_broxy.START_SESSION_BROXY_ENV, '1')
  monkeypatch.setenv('BROKER_CHANNEL', 'tcp://root-token@127.0.0.1:7')

  with ride_broxy.session_broxy():
    assert os.environ['BROKER_CHANNEL'] == daemon.address
    assert ride_broxy.START_SESSION_BROXY_ENV not in os.environ

  start.assert_called_once()
  daemon.stop.assert_called_once()
  assert os.environ['BROKER_CHANNEL'] == 'tcp://root-token@127.0.0.1:7'


def test_session_broxy_leaves_an_existing_session_channel_alone(monkeypatch):
  start = MagicMock()
  monkeypatch.setattr(ride_broxy, '_start_session_broxy', start)
  monkeypatch.delenv(ride_broxy.START_SESSION_BROXY_ENV, raising=False)
  monkeypatch.setenv('BROKER_CHANNEL', 'tcp://existing-token@127.0.0.1:6')

  with ride_broxy.session_broxy():
    assert os.environ['BROKER_CHANNEL'] == 'tcp://existing-token@127.0.0.1:6'

  start.assert_not_called()
