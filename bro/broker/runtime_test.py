from dataclasses import dataclass
from typing import cast

import pytest

from bro.broker import brotocol
from bro.broker.runtime import Runtime
from bro.broker.spawn import LaunchSpec, Spawner
from bro.broker.transport import Provisioned, ServerTransport
from bro.broker.transports.tcp import Endpoint


class FakeTransport:
  def __init__(self):
    self.sink = None
    self.sent = []
    self.closed = []
    self.stopped = False

  async def provision(self):
    return Provisioned('channel', Endpoint(1234, 'token'))

  async def serve(self, sink):
    self.sink = sink

  async def send(self, channel, message):
    self.sent.append((channel, message))

  async def close(self, channel):
    self.closed.append(channel)

  async def shutdown(self):
    self.stopped = True


@dataclass
class FakeHandle:
  code: int = 0

  async def wait(self):
    return self.code

  async def kill(self):
    pass

  def output_tail(self):
    return ''


class FakeSpawner:
  def __init__(self):
    self.calls = []
    self.handle = FakeHandle()

  async def spawn(self, launch, provisioned, quest):
    self.calls.append((launch, provisioned, quest))
    return self.handle


class ChannelEvents:
  def __init__(self):
    self.connected = 0
    self.messages = []
    self.disconnected = 0

  def on_connect(self):
    self.connected += 1

  def on_message(self, message):
    self.messages.append(message)

  def on_disconnect(self):
    self.disconnected += 1


@pytest.mark.asyncio
async def test_runtime_demultiplexes_channel_events_and_messages():
  transport = FakeTransport()
  runtime = Runtime(cast(ServerTransport, transport), cast(Spawner, FakeSpawner()))
  events = ChannelEvents()
  provisioned = await runtime.provision(events)
  message = brotocol.request('ping', {})
  await runtime.on_connect(provisioned.channel)
  await runtime.on_message(provisioned.channel, message)
  await runtime.on_disconnect(provisioned.channel)
  assert events.connected == 1
  assert events.disconnected == 1
  assert events.messages == [message]


@pytest.mark.asyncio
async def test_runtime_launches_through_the_spawn_port():
  transport = FakeTransport()
  spawner = FakeSpawner()
  runtime = Runtime(cast(ServerTransport, transport), cast(Spawner, spawner))
  provisioned = await runtime.provision(ChannelEvents())
  handle = await runtime.launch(LaunchSpec(), provisioned, 'quest')
  assert handle is spawner.handle
  assert spawner.calls[0][1:] == (provisioned, 'quest')


@pytest.mark.asyncio
async def test_send_close_and_stop_delegate_to_the_transport():
  transport = FakeTransport()
  runtime = Runtime(cast(ServerTransport, transport), cast(Spawner, FakeSpawner()))
  await runtime.provision(ChannelEvents())
  message = brotocol.result('quest', 'ok')
  runtime.send('channel', message)
  await __import__('asyncio').sleep(0)
  await runtime.close('channel')
  await runtime.stop()
  assert transport.sent == [('channel', message)]
  assert transport.closed == ['channel']
  assert transport.stopped


@pytest.mark.asyncio
async def test_unknown_channels_are_ignored():
  runtime = Runtime(cast(ServerTransport, FakeTransport()), cast(Spawner, FakeSpawner()))
  await runtime.on_connect('missing')
  await runtime.on_message('missing', brotocol.request('ping', {}))
  await runtime.on_disconnect('missing')
