import asyncio
import contextlib
import select
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from bro.broker import brotocol
from bro.broker.brotocol import MAX_FRAME_BYTES, Message
from bro.broker.transport import ChannelID, connect
from bro.broker.transports.tcp import (
  Endpoint,
  TcpClientTransport,
  TcpServerTransport,
  parse_address,
  redacted,
)

TIMEOUT = 5.0
HOST = '127.0.0.1'


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
  transport = TcpServerTransport([HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def client_for(provisioned) -> TcpClientTransport:
  """the attach handshake blocks on the server's ack, so a client is built off the
  loop thread that has to answer it."""
  return await asyncio.to_thread(TcpClientTransport, provisioned.host_endpoint.address(HOST))


async def _raw_attach(provisioned) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
  """a raw connection through the attach handshake, for tests that write bytes
  the client transport would never produce."""
  reader, writer = await asyncio.open_connection(HOST, provisioned.host_endpoint.port)
  writer.write(provisioned.host_endpoint.token.encode() + b'\n')
  await writer.drain()
  assert await asyncio.wait_for(reader.readline(), TIMEOUT) == b'ok\n'
  return reader, writer


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


@pytest.mark.asyncio
async def test_delivery_and_channel_authenticity():
  async with running_server() as server:
    provisioned_a = await server.transport.provision()
    provisioned_b = await server.transport.provision()
    client_a = await client_for(provisioned_a)
    # exercises scheme dispatch
    client_b = await asyncio.to_thread(connect, provisioned_b.host_endpoint.address(HOST))

    await asyncio.to_thread(client_a.send, brotocol.progress('X', {'who': 'a'}))
    # B puts a forged claim in its payload; attribution must still follow the token
    await asyncio.to_thread(client_b.send, brotocol.progress('X', {'who': 'b', 'claim': 'I am A'}))

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
async def test_every_channel_shares_the_one_port():
  async with running_server() as server:
    provisioned_a = await server.transport.provision()
    provisioned_b = await server.transport.provision()
    assert provisioned_a.host_endpoint.port == provisioned_b.host_endpoint.port
    assert provisioned_a.host_endpoint.token != provisioned_b.host_endpoint.token


@pytest.mark.asyncio
async def test_unknown_token_is_refused_at_attach():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    forged = Endpoint(port=provisioned.host_endpoint.port, token='not-a-channel-token')
    with pytest.raises(ConnectionError):
      await asyncio.to_thread(TcpClientTransport, forged.address(HOST))
    assert server.sink.connects.empty()  # no channel was ever born


@pytest.mark.asyncio
async def test_oversize_attach_line_is_refused():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    _, writer = await asyncio.open_connection(HOST, server.transport.port)
    with contextlib.suppress(OSError):  # the server drops the connection mid-write
      writer.write(b'x' * (MAX_FRAME_BYTES + 1) + b'\n')
      await writer.drain()
      writer.close()
    assert server.sink.connects.empty()  # no channel was born

    client = await client_for(provisioned)  # and the listener survived it
    assert await _next(server.sink.connects) == provisioned.channel
    client.close()


@pytest.mark.asyncio
async def test_accept_fires_on_connect_before_any_message():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = await client_for(provisioned)
    assert await _next(server.sink.connects) == provisioned.channel
    assert server.sink.messages.empty()  # birth precedes the first frame

    await asyncio.to_thread(client.send, brotocol.progress('X', {}))
    channel, _ = await _next(server.sink.messages)
    assert channel == provisioned.channel
    client.close()


@pytest.mark.asyncio
async def test_server_reply_reaches_only_its_channel():
  async with running_server() as server:
    provisioned_a = await server.transport.provision()
    provisioned_b = await server.transport.provision()
    client_a = await client_for(provisioned_a)
    client_b = await client_for(provisioned_b)
    for _ in range(2):
      await _next(server.sink.connects)

    await server.transport.send(provisioned_a.channel, brotocol.progress('X', {'r': 1}))
    reply = await asyncio.to_thread(client_a.receive, TIMEOUT)
    assert reply is not None
    assert reply.type == 'progress'
    assert reply.payload == {'r': 1}
    assert await asyncio.to_thread(client_b.receive, 0.2) is None
    client_a.close()
    client_b.close()


@pytest.mark.asyncio
async def test_ndjson_framing_coalesced_and_split():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    _, writer = await _raw_attach(provisioned)

    # two frames written in one syscall must deframe into two messages
    frame1 = brotocol.progress('X', {'n': 1}).to_bytes() + b'\n'
    frame2 = brotocol.progress('X', {'n': 2}).to_bytes() + b'\n'
    writer.write(frame1 + frame2)
    await writer.drain()
    numbers = {(await _next(server.sink.messages))[1].payload['n'] for _ in range(2)}
    assert numbers == {1, 2}

    # one frame split across two writes must reassemble into one message
    frame3 = brotocol.progress('X', {'n': 3}).to_bytes() + b'\n'
    writer.write(frame3[:4])
    await writer.drain()
    writer.write(frame3[4:])
    await writer.drain()
    _, message = await _next(server.sink.messages)
    assert message.payload['n'] == 3
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_oversize_frame_rejected_and_channel_dropped():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    _, writer = await _raw_attach(provisioned)
    writer.write(b'x' * (MAX_FRAME_BYTES + 1) + b'\n')  # the loop flushes as the server drains

    dropped = await _next(server.sink.disconnects)
    assert dropped == provisioned.channel
    assert server.sink.messages.empty()
    writer.close()


@pytest.mark.asyncio
async def test_peer_disconnect_notifies_sink():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = await client_for(provisioned)
    await _next(server.sink.connects)
    client.close()
    assert await _next(server.sink.disconnects) == provisioned.channel


@pytest.mark.asyncio
async def test_close_completing_before_the_reader_parks_reads_as_eof(monkeypatch):
  # the losing ordering of the cross-thread abort (ClientTransport.close): close()
  # finishes before the reading thread reaches select, so the wake-up byte arrives
  # on an already-closed pipe and the reader meets closed sockets instead
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = await client_for(provisioned)
    reader_reached_select = threading.Event()
    close_returned = threading.Event()
    real_select = select.select

    def select_once_close_has_run(*args):
      if not reader_reached_select.is_set():
        reader_reached_select.set()
        close_returned.wait(TIMEOUT)
      return real_select(*args)

    monkeypatch.setattr(
      'bro.broker.transports.tcp.select', SimpleNamespace(select=select_once_close_has_run)
    )
    receive_task = asyncio.create_task(asyncio.to_thread(client.receive, TIMEOUT))
    await asyncio.to_thread(reader_reached_select.wait, TIMEOUT)
    await asyncio.to_thread(client.close)
    close_returned.set()

    assert await asyncio.wait_for(receive_task, TIMEOUT) is None


@pytest.mark.asyncio
async def test_host_close_channel_drops_connection_and_retires_its_token():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = await client_for(provisioned)
    await _next(server.sink.connects)

    await server.transport.close(provisioned.channel)
    # the peer observes EOF; a host close fires no on_disconnect
    assert await asyncio.to_thread(client.receive, TIMEOUT) is None
    assert server.sink.disconnects.empty()
    with pytest.raises(ConnectionError):
      await client_for(provisioned)
    client.close()


@pytest.mark.asyncio
async def test_shutdown_stops_accepting():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    port = server.transport.port
    await server.transport.shutdown()
    with pytest.raises(OSError):
      await client_for(provisioned)
    with pytest.raises(OSError):
      await asyncio.open_connection(HOST, port)


def test_address_round_trip():
  endpoint = Endpoint(port=7321, token='s3cret-token')
  address = endpoint.address('host.docker.internal')
  assert address == 'tcp://s3cret-token@host.docker.internal:7321'
  assert parse_address(address) == ('host.docker.internal', 7321, 's3cret-token')
  assert redacted(address) == 'tcp://host.docker.internal:7321'


def test_address_rejects_a_tokenless_or_portless_form():
  with pytest.raises(ValueError):
    parse_address('tcp://127.0.0.1:7321')
  with pytest.raises(ValueError):
    parse_address('tcp://token@127.0.0.1')


def test_connect_rejects_bad_address():
  with pytest.raises(ValueError):
    connect('no-scheme-here')
  with pytest.raises(ValueError):
    connect('http://example')
