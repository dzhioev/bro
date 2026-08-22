"""tcp transport adapter — the host↔peer substrate.

This adapter owns *framing*: NDJSON (`Message.to_bytes() + '\\n'`), on top of
brotocol's encoding. One listening port serves every channel, bound on each host
the constructor names — all on the same port, so a single port number reaches
the broker from wherever a peer runs.

Channel authenticity: a connection opens with its channel's token alone on the
first line, and the server answers `ok` before any message flows. The token is
what the server attributes the connection by; the peer can put nothing on the
wire that changes that, and there is no `from` field to forge. A connection that
opens with an unknown token, an oversize line, or nothing at all within
`_ATTACH_TIMEOUT` is closed without an answer. The ack is what makes a refused
attach raise at the client's constructor instead of silently swallowing
everything the peer goes on to send.

Concurrency — one event loop, no locks. The server is asyncio-native: each
accepted connection attaches, fires `Sink.on_connect`, then runs a read task that
NDJSON-deframes frames into the `Sink`, and `send()` writes through that
connection's `StreamWriter`. Because everything runs on the single loop, the
shared per-channel state needs no lock and two coroutines can never interleave a
partial NDJSON frame. A peer that stops reading is absorbed by its own writer's
`drain()` backpressure, never by stalling routing to the other peers.

`TcpClientTransport` is synchronous: a peer is a separate process with no event
loop of its own.
"""

import asyncio
import contextlib
import secrets
import select
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from bro.base import log
from bro.base.lulid import lulid
from bro.broker.brotocol import MAX_FRAME_BYTES, Message, ProtocolError
from bro.broker.transport import (
  Address,
  ChannelID,
  ClientTransport,
  Provisioned,
  ServerTransport,
  Sink,
)

SCHEME = 'tcp'
LOCAL_HOST = '127.0.0.1'

_LISTEN_BACKLOG = 16
_READ_CHUNK = 65536
_TOKEN_BYTES = 32
_ATTACH_TIMEOUT = 30.0
_ATTACH_ACK = b'ok'
_MAX_ATTACH_LINE = 4096  # the client's bound on the ack; the server's is the stream limit
_BIND_ATTEMPTS = 8  # a port free on the first host may be taken on a later one


@dataclass(frozen=True)
class Endpoint:
  """what a provisioned channel is reachable at: `address` renders it for a peer
  that reaches the host under `host` — loopback for one running beside it, a
  routable name for one that reaches it from elsewhere."""

  port: int
  token: str

  def address(self, host: str) -> Address:
    return f'{SCHEME}://{self.token}@{host}:{self.port}'


def parse_address(address: Address) -> tuple[str, int, str]:
  """split a channel address into host, port, and token."""
  parts = urlsplit(address)
  if parts.scheme != SCHEME or parts.hostname is None or parts.port is None:
    raise ValueError(f'not a broker tcp address: {address!r}')
  if parts.username is None or len(parts.username) == 0:
    raise ValueError(f'broker tcp address carries no channel token: {address!r}')
  return parts.hostname, parts.port, parts.username


def redacted(address: Address) -> str:
  """the address with its channel token dropped — an address is a credential, so
  this is the form that goes in a log."""
  host, port, _ = parse_address(address)
  return f'{SCHEME}://{host}:{port}'


async def open_channel(
  address: Address, *, limit: int = _READ_CHUNK
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
  """dial a channel address on the event loop, through the attach handshake —
  the asyncio counterpart of `TcpClientTransport` for a peer that already has a
  loop of its own."""
  host, port, token = parse_address(address)
  reader, writer = await asyncio.open_connection(host, port, limit=limit)
  writer.write(token.encode() + b'\n')
  await writer.drain()
  ack = await asyncio.wait_for(reader.readline(), _ATTACH_TIMEOUT)
  if ack.strip() != _ATTACH_ACK:
    writer.close()
    raise ConnectionError(f'channel {redacted(address)} refused the attach')
  return reader, writer


async def read_attach_token(reader: asyncio.StreamReader) -> Optional[str]:
  """the token a connection opens with — None when none arrived within
  `_ATTACH_TIMEOUT`, the line ran past the stream limit, or the peer left."""
  try:
    line = await asyncio.wait_for(reader.readline(), _ATTACH_TIMEOUT)
  except (TimeoutError, ValueError, OSError) as error:
    log.warning('broker: dropping a connection that did not attach (%s)', error)
    return None
  if len(line) == 0:
    return None
  return line.strip().decode('utf-8', errors='replace')


async def acknowledge_attach(writer: asyncio.StreamWriter) -> bool:
  """answer an accepted attach; False when the peer went away first."""
  writer.write(_ATTACH_ACK + b'\n')
  try:
    await writer.drain()
  except OSError as error:
    log.warning('broker: a connection left before its attach was acknowledged (%s)', error)
    return False
  return True


@dataclass
class _Connection:
  writer: asyncio.StreamWriter
  # the read task; cancelled on a host-side close/shutdown to suppress its EOF on_disconnect
  task: asyncio.Task


def _bind(hosts: Sequence[str]) -> list[socket.socket]:
  """bind every host on one shared ephemeral port, chosen by the first. The port
  the first host lands on can be taken on a later one, so a collision retries the
  whole set on a fresh port."""
  first, rest = hosts[0], hosts[1:]
  collision: Optional[OSError] = None
  for _ in range(_BIND_ATTEMPTS):
    bound = [_bind_one(first, 0)]  # an unbindable first host is the caller's error, not a retry
    port = bound[0].getsockname()[1]
    try:
      bound += [_bind_one(host, port) for host in rest]
      return bound
    except OSError as error:
      collision = error
      for sock in bound:
        sock.close()
  raise OSError(f'no port free on all of {", ".join(hosts)}: {collision}')


def _bind_one(host: str, port: int) -> socket.socket:
  family, socket_type, protocol, _, address = socket.getaddrinfo(
    host, port, type=socket.SOCK_STREAM
  )[0]
  sock = socket.socket(family, socket_type, protocol)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  sock.bind(address)
  return sock


class TcpServerTransport(ServerTransport):
  def __init__(self, bind_hosts: Sequence[str]):
    if len(bind_hosts) == 0:
      raise ValueError('a tcp server transport needs at least one bind host')
    self._bind_hosts = tuple(bind_hosts)
    self._sink: Optional[Sink] = None
    self._servers: list[asyncio.Server] = []
    self._port: Optional[int] = None
    self._channels: dict[str, ChannelID] = {}  # token → channel
    self._tokens: dict[ChannelID, str] = {}  # channel → token
    self._connections: dict[ChannelID, _Connection] = {}
    self._stopped = asyncio.Event()

  @property
  def port(self) -> int:
    """the port every channel is served on; only bound once something is provisioned."""
    if self._port is None:
      raise RuntimeError('the broker transport is not listening yet')
    return self._port

  @property
  def channels(self) -> frozenset[ChannelID]:
    """the channels provisioned right now — one leaves on close or shutdown, and
    its address then attaches to nothing."""
    return frozenset(self._tokens.copy())

  async def provision(self) -> Provisioned:
    port = await self._listen()
    channel = lulid()
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    self._channels[token] = channel
    self._tokens[channel] = token
    return Provisioned(channel=channel, host_endpoint=Endpoint(port=port, token=token))

  async def serve(self, sink: Sink) -> None:
    self._sink = sink
    await self._stopped.wait()  # the listeners accept in the background on this loop

  async def send(self, channel: ChannelID, message: Message) -> None:
    frame = message.to_bytes()
    if len(frame) > MAX_FRAME_BYTES:
      raise ProtocolError(f'outbound message is {len(frame)} bytes, over {MAX_FRAME_BYTES}')
    connection = self._connections.get(channel)
    if connection is None:
      log.warning(f'broker: dropping outbound message to unconnected channel {channel}')
      return
    connection.writer.write(frame + b'\n')
    await connection.writer.drain()

  async def close(self, channel: ChannelID) -> None:
    await self._drop_connection(channel)
    token = self._tokens.pop(channel, None)
    if token is not None:
      self._channels.pop(token, None)

  async def shutdown(self) -> None:
    for channel in list(self._connections):
      await self._drop_connection(channel)
    for server in self._servers:
      server.close()
      await server.wait_closed()
    self._servers = []
    self._port = None
    self._channels.clear()
    self._tokens.clear()
    self._stopped.set()

  # --- connection handling (all on the loop) -------------------------------

  async def _listen(self) -> int:
    if self._port is not None:
      return self._port
    sockets = _bind(self._bind_hosts)
    self._servers = [
      await asyncio.start_server(self._serve_connection, sock=sock, backlog=_LISTEN_BACKLOG)
      for sock in sockets
    ]
    port = sockets[0].getsockname()[1]
    self._port = port
    log.verbose('broker: listening on %s port %d', ', '.join(self._bind_hosts), port)
    return port

  async def _serve_connection(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
  ) -> None:
    channel = await self._attach(reader, writer)
    if channel is None:
      writer.close()
      return
    prior = self._connections.get(channel)
    if prior is not None:  # a reconnect supersedes the prior connection, no disconnect notice
      prior.task.cancel()
      prior.writer.close()
    task = asyncio.current_task()
    assert task is not None
    connection = _Connection(writer=writer, task=task)
    self._connections[channel] = connection
    assert self._sink is not None
    await self._sink.on_connect(channel)
    try:
      await self._read_loop(channel, reader)
    finally:
      if self._connections.get(channel) is connection:  # not already superseded/dropped
        del self._connections[channel]
      writer.close()

  async def _attach(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
  ) -> Optional[ChannelID]:
    token = await read_attach_token(reader)
    if token is None:
      return None
    channel = self._channels.get(token)
    if channel is None:
      log.warning('broker: dropping a connection that attached with an unknown token')
      return None
    return channel if await acknowledge_attach(writer) else None

  async def _read_loop(self, channel: ChannelID, reader: asyncio.StreamReader) -> None:
    read_buffer = bytearray()
    while True:
      try:
        data = await reader.read(_READ_CHUNK)
      except OSError:
        await self._notify_disconnect(channel)
        return
      if len(data) == 0:  # peer closed
        await self._notify_disconnect(channel)
        return
      read_buffer += data
      while True:
        newline_index = read_buffer.find(b'\n')
        if newline_index < 0:
          if len(read_buffer) > MAX_FRAME_BYTES:  # an unterminated frame already over the cap
            await self._reject_oversize(channel)
            return
          break
        frame = bytes(read_buffer[:newline_index])
        del read_buffer[: newline_index + 1]
        if len(frame) > MAX_FRAME_BYTES:
          await self._reject_oversize(channel)
          return
        try:
          message = Message.from_bytes(frame)
        except ProtocolError:
          log.warning(f'broker: channel {channel} sent a malformed frame, dropping channel')
          await self._notify_disconnect(channel)
          return
        assert self._sink is not None
        await self._sink.on_message(channel, message)

  async def _reject_oversize(self, channel: ChannelID) -> None:
    log.warning(f'broker: channel {channel} sent a frame over {MAX_FRAME_BYTES} bytes, dropping')
    await self._notify_disconnect(channel)

  async def _notify_disconnect(self, channel: ChannelID) -> None:
    if self._sink is not None:
      await self._sink.on_disconnect(channel)

  async def _drop_connection(self, channel: ChannelID) -> None:
    """host-side drop (close/shutdown/supersede): cancel the read task so its EOF
    does not fire on_disconnect, then close the writer and wait the task out."""
    connection = self._connections.pop(channel, None)
    if connection is None:
      return
    connection.task.cancel()
    connection.writer.close()
    await asyncio.gather(connection.task, return_exceptions=True)


class TcpClientTransport(ClientTransport):
  def __init__(self, address: Address):
    host, port, token = parse_address(address)
    self._read_buffer = bytearray()
    with contextlib.ExitStack() as stack:
      self._sock = stack.enter_context(
        socket.create_connection((host, port), timeout=_ATTACH_TIMEOUT)
      )
      self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
      self._attach(token)
      self._sock.settimeout(None)
      # self-pipe pair: close() sends a byte to wake a receive() blocked in select
      # on another thread — the cross-thread abort ClientTransport.close guarantees.
      # A shutdown() alone is not enough: macOS does not reliably wake a parked
      # poll/select when the socket is shut down from another thread
      self._abort_receive, self._abort_send = socket.socketpair()
      stack.enter_context(contextlib.closing(self._abort_receive))
      stack.enter_context(contextlib.closing(self._abort_send))
      stack.pop_all()

  def _attach(self, token: str) -> None:
    self._sock.sendall(token.encode() + b'\n')
    while True:
      newline_index = self._read_buffer.find(b'\n')
      if newline_index >= 0:
        break
      if len(self._read_buffer) > _MAX_ATTACH_LINE:
        raise ConnectionError('broker channel answered the attach with an oversize line')
      try:
        data = self._sock.recv(_READ_CHUNK)
      except OSError as error:
        raise ConnectionError(f'broker channel refused the attach: {error}') from error
      if len(data) == 0:
        raise ConnectionError('broker channel refused the attach')
      self._read_buffer += data
    line = bytes(self._read_buffer[:newline_index])
    del self._read_buffer[: newline_index + 1]
    if line != _ATTACH_ACK:
      raise ConnectionError(f'broker channel answered the attach with {line!r}')

  def send(self, message: Message) -> None:
    frame = message.to_bytes()
    if len(frame) > MAX_FRAME_BYTES:
      raise ProtocolError(f'outbound message is {len(frame)} bytes, over {MAX_FRAME_BYTES}')
    self._sock.sendall(frame + b'\n')

  def receive(self, timeout: Optional[float]) -> Optional[Message]:
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
      message = self._take_frame()
      if message is not None:
        return message
      remaining = None
      if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          return None
      try:
        readable, _, _ = select.select([self._sock, self._abort_receive], [], [], remaining)
      except (OSError, ValueError):
        # the socket died under us — a concurrent close() aborting this wait
        # (see ClientTransport.close), or the peer tearing the channel down
        # mid-read; either way the channel is gone, which is EOF to the caller.
        # A close() that completed before this thread parked leaves closed socket
        # objects (fileno() == -1), which select rejects with ValueError, not OSError
        return None
      if self._abort_receive in readable:  # close() aborted this wait
        return None
      if len(readable) == 0:  # timeout
        return None
      try:
        data = self._sock.recv(_READ_CHUNK)
      except OSError:
        return None
      if len(data) == 0:  # clean EOF, no in-flight frame
        return None
      self._read_buffer += data

  def _take_frame(self) -> Optional[Message]:
    newline_index = self._read_buffer.find(b'\n')
    if newline_index < 0:
      if len(self._read_buffer) > MAX_FRAME_BYTES:
        raise ProtocolError(f'inbound frame over {MAX_FRAME_BYTES} bytes')
      return None
    frame = bytes(self._read_buffer[:newline_index])
    del self._read_buffer[: newline_index + 1]
    if len(frame) > MAX_FRAME_BYTES:
      raise ProtocolError(f'inbound frame over {MAX_FRAME_BYTES} bytes')
    return Message.from_bytes(frame)

  def close(self, confirm: bool = False) -> None:
    try:
      self._abort_send.send(b'x')  # wake any receive() blocked on another thread
    except OSError:
      pass  # already closed
    if confirm:
      # half-close handshake (see ClientTransport.close): signal EOF, then wait
      # for the receiver to close back. Late inbound is discarded — the caller is
      # closing, only the EOF matters.
      try:
        self._sock.shutdown(socket.SHUT_WR)
        self._sock.settimeout(None)
        while len(self._sock.recv(_READ_CHUNK)) > 0:
          pass
      except OSError:
        pass  # receiver already gone — nothing left to confirm
    else:
      try:
        self._sock.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass  # already shut down, or the peer is gone
    self._sock.close()
    self._abort_send.close()
    self._abort_receive.close()
