"""live test of the host-mode session broxy helper (`cw.broxy`): a real
`broxy serve` subprocess — the console script resolved from the active venv —
against a real provisioned upstream socket, plus the no-channel degrade paths
that keep a launch alive without one."""

import asyncio
import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import cw.broxy
from broker.brotocol import Message
from broker.client import Client
from broker.transport import ChannelID
from broker.transports.unix import UnixClientTransport, UnixServerTransport
from cw.broxy import _start_session_broxy

TIMEOUT = 10.0


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
async def running_server():
  # sockets live in a short mkdtemp dir, not pytest's tmp_path: the lulid-named
  # channel socket under the deep per-test dirs exceeds the ~108-byte sun_path cap
  socket_dir = Path(tempfile.mkdtemp(prefix='broxy-'))
  transport = UnixServerTransport(str(socket_dir / 'upstream'))
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_broxy_serves_the_rewritten_channel():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    broxy = await asyncio.to_thread(
      _start_session_broxy, 'unix:' + str(provisioned.host_endpoint), os.environ
    )
    assert broxy is not None
    try:
      scheme, _, socket_path = broxy.address.partition(':')
      assert scheme == 'unix'
      client = Client(UnixClientTransport(socket_path))
      request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
      channel, message = await asyncio.wait_for(server.sink.messages.get(), TIMEOUT)
      assert channel == provisioned.channel  # the proxy rides the session's one channel
      assert message.type == 'ping'
      await server.transport.send(
        channel, Message(type='reply', payload={'pong': {}}, in_reply_to=message.id)
      )
      reply = await asyncio.wait_for(request_task, TIMEOUT)
      assert reply.type == 'reply'
      assert reply.in_reply_to == message.id
      client.close()
    finally:
      broxy.stop()


def test_start_returns_none_when_the_upstream_is_unreachable(tmp_path, monkeypatch):
  monkeypatch.setattr(cw.broxy, '_READY_TIMEOUT', 1.0)
  broxy = _start_session_broxy('unix:' + str(tmp_path / 'missing.sock'), os.environ)
  assert broxy is None


def test_start_returns_none_without_the_console_script(tmp_path):
  # a venv without broxy (a workspace based on a pre-broxy ref): the serve
  # spawn itself fails, and the caller is left to unset BROKER_CHANNEL
  broxy = _start_session_broxy('unix:' + str(tmp_path / 'upstream.sock'), {'PATH': str(tmp_path)})
  assert broxy is None
