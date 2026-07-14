import asyncio
import contextlib
import os
import stat
from dataclasses import dataclass

import pytest

from broker.brotocol import MAX_FRAME_BYTES, Message
from broker.transport import ChannelID, connect
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
  control_dir: str


@contextlib.asynccontextmanager
async def running_server(socket_dir):
  control_dir = str(socket_dir)
  transport = UnixServerTransport(control_dir)
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Harness(transport=transport, sink=sink, control_dir=control_dir)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


@pytest.mark.asyncio
async def test_delivery_and_channel_authenticity(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned_a = await server.transport.provision()
    provisioned_b = await server.transport.provision()
    client_a = UnixClientTransport(provisioned_a.host_endpoint)
    client_b = connect('unix:' + provisioned_b.host_endpoint)  # exercises scheme dispatch

    await asyncio.to_thread(client_a.send, Message(type='ping', payload={'who': 'a'}))
    # B puts a forged claim in its payload; attribution must still follow the socket
    await asyncio.to_thread(
      client_b.send, Message(type='ping', payload={'who': 'b', 'claim': 'I am A'})
    )

    seen = {}
    for _ in range(2):
      channel, message = await _next(server.sink.messages)
      seen[message.payload['who']] = channel

    assert seen['a'] == provisioned_a.channel
    assert seen['b'] == provisioned_b.channel
    assert provisioned_a.channel != provisioned_b.channel
    client_a.close()
    client_b.close()


@pytest.mark.asyncio
async def test_accept_fires_on_connect_before_any_message(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = UnixClientTransport(provisioned.host_endpoint)
    assert await _next(server.sink.connects) == provisioned.channel
    assert server.sink.messages.empty()  # birth precedes the first frame

    await asyncio.to_thread(client.send, Message(type='ping', payload={}))
    channel, _ = await _next(server.sink.messages)
    assert channel == provisioned.channel
    client.close()


@pytest.mark.asyncio
async def test_server_reply_reaches_only_its_channel(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned_a = await server.transport.provision()
    provisioned_b = await server.transport.provision()
    client_a = UnixClientTransport(provisioned_a.host_endpoint)
    client_b = UnixClientTransport(provisioned_b.host_endpoint)
    # send so the loop accepts both connections and learns their channels
    await asyncio.to_thread(client_a.send, Message(type='ping', payload={}))
    await asyncio.to_thread(client_b.send, Message(type='ping', payload={}))
    for _ in range(2):
      await _next(server.sink.messages)

    await server.transport.send(provisioned_a.channel, Message(type='pong', payload={'r': 1}))
    reply = await asyncio.to_thread(client_a.receive, TIMEOUT)
    assert reply is not None
    assert reply.type == 'pong'
    assert reply.payload == {'r': 1}
    assert await asyncio.to_thread(client_b.receive, 0.2) is None
    client_a.close()
    client_b.close()


@pytest.mark.asyncio
async def test_ndjson_framing_coalesced_and_split(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    _, writer = await asyncio.open_unix_connection(provisioned.host_endpoint)

    # two frames written in one syscall must deframe into two messages
    frame1 = Message(type='ping', payload={'n': 1}).to_bytes() + b'\n'
    frame2 = Message(type='ping', payload={'n': 2}).to_bytes() + b'\n'
    writer.write(frame1 + frame2)
    await writer.drain()
    numbers = {(await _next(server.sink.messages))[1].payload['n'] for _ in range(2)}
    assert numbers == {1, 2}

    # one frame split across two writes must reassemble into one message
    frame3 = Message(type='ping', payload={'n': 3}).to_bytes() + b'\n'
    writer.write(frame3[:4])
    await writer.drain()
    writer.write(frame3[4:])
    await writer.drain()
    _, message = await _next(server.sink.messages)
    assert message.payload['n'] == 3
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_oversize_frame_rejected_and_channel_dropped(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    _, writer = await asyncio.open_unix_connection(provisioned.host_endpoint)
    writer.write(b'x' * (MAX_FRAME_BYTES + 1) + b'\n')  # the loop flushes as the server drains

    dropped = await _next(server.sink.disconnects)
    assert dropped == provisioned.channel
    assert server.sink.messages.empty()
    writer.close()


@pytest.mark.asyncio
async def test_peer_disconnect_notifies_sink(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = UnixClientTransport(provisioned.host_endpoint)
    await asyncio.to_thread(client.send, Message(type='ping', payload={}))
    await _next(server.sink.messages)
    client.close()
    assert await _next(server.sink.disconnects) == provisioned.channel


@pytest.mark.asyncio
async def test_socket_lifecycle_perms_and_teardown(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    sock_path = provisioned.host_endpoint
    assert stat.S_ISSOCK(os.stat(sock_path).st_mode)
    assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(server.control_dir).st_mode) == 0o700

    await server.transport.shutdown()  # unlinks synchronously before returning
    assert not os.path.exists(sock_path)


@pytest.mark.asyncio
async def test_host_close_channel_drops_connection(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = UnixClientTransport(provisioned.host_endpoint)
    await asyncio.to_thread(client.send, Message(type='ping', payload={}))
    await _next(server.sink.messages)

    await server.transport.close(provisioned.channel)
    # the peer observes EOF and the socket file is gone; a host close fires no on_disconnect
    assert await asyncio.to_thread(client.receive, TIMEOUT) is None
    assert not os.path.exists(provisioned.host_endpoint)
    assert server.sink.disconnects.empty()
    client.close()


def test_connect_rejects_bad_address():
  with pytest.raises(ValueError):
    connect('no-scheme-here')
  with pytest.raises(ValueError):
    connect('http://example')
