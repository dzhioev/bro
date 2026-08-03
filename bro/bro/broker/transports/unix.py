"""unix-socket transport adapter — the v1 host↔peer substrate.

This adapter owns *framing*: NDJSON (`Message.to_bytes() + '\\n'`), on top of
brotocol's encoding. One bound socket file per peer under the control dir
(`<control_dir>/<channel>.sock`), passed in by the constructor rather than hardcoded.

Concurrency — one event loop, no locks. The server is asyncio-native: `provision()`
starts one `asyncio.start_unix_server` per channel; each accepted connection fires
`Sink.on_connect` then runs a read task that NDJSON-deframes frames into the `Sink`,
and `send()` writes through that connection's `StreamWriter`. Because everything runs
on the single loop, the shared per-channel state needs no lock and two coroutines can
never interleave a partial NDJSON frame. A peer that stops reading is absorbed by its
own writer's `drain()` backpressure, never by stalling routing to the other peers.

`provision()` binds and listens the socket before returning (so the socket file
exists before the endpoint is handed out and a peer connects — the launch
bind-mounts the now-existing file into the container).

Channel authenticity: a connection is attributed to the `ChannelID` of the
listening socket that accepted it. The peer can put nothing on the wire that
changes that — there is no `from` field to forge.

`UnixClientTransport` is synchronous: a peer is a separate process with no event
loop of its own.
"""

import asyncio
import os
import select
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.base.lulid import lulid
from bro.broker.brotocol import MAX_FRAME_BYTES, Message, ProtocolError
from bro.broker.transport import ChannelID, ClientTransport, Provisioned, ServerTransport, Sink

_LISTEN_BACKLOG = 16
_READ_CHUNK = 65536


@dataclass
class _Connection:
  writer: asyncio.StreamWriter
  # the read task; cancelled on a host-side close/shutdown to suppress its EOF on_disconnect
  task: asyncio.Task


class UnixServerTransport(ServerTransport):
  def __init__(self, control_dir: str):
    self._dir = Path(control_dir)
    self._sink: Optional[Sink] = None
    self._servers: dict[ChannelID, asyncio.Server] = {}
    self._paths: dict[ChannelID, Path] = {}
    self._connections: dict[ChannelID, _Connection] = {}
    self._stopped = asyncio.Event()

  async def provision(self) -> Provisioned:
    self._dir.mkdir(parents=True, exist_ok=True)
    os.chmod(self._dir, 0o700)
    channel = lulid()
    path = self._dir / f'{channel}.sock'
    path.unlink(missing_ok=True)  # stale socket from a crashed prior run
    server = await asyncio.start_unix_server(
      lambda reader, writer: self._serve_connection(channel, reader, writer),
      path=str(path),  # ENAMETOOLONG here if the host path exceeds sun_path (~108 bytes)
      backlog=_LISTEN_BACKLOG,
    )
    os.chmod(path, 0o600)
    self._servers[channel] = server
    self._paths[channel] = path
    return Provisioned(channel=channel, host_endpoint=str(path))

  async def serve(self, sink: Sink) -> None:
    self._sink = sink
    await self._stopped.wait()  # the per-channel servers accept in the background on this loop

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
    server = self._servers.pop(channel, None)
    path = self._paths.pop(channel, None)
    if server is not None:
      server.close()
      await server.wait_closed()
    if path is not None:
      path.unlink(missing_ok=True)

  async def shutdown(self) -> None:
    for channel in list(self._connections):
      await self._drop_connection(channel)
    for channel in list(self._servers):
      server = self._servers.pop(channel)
      server.close()
      await server.wait_closed()
      path = self._paths.pop(channel, None)
      if path is not None:
        path.unlink(missing_ok=True)
    self._stopped.set()

  # --- connection handling (all on the loop) -------------------------------

  async def _serve_connection(
    self, channel: ChannelID, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
  ) -> None:
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


class UnixClientTransport(ClientTransport):
  def __init__(self, path: str):
    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    self._sock.connect(path)
    self._read_buffer = bytearray()
    # self-pipe pair: close() sends a byte to wake a receive() blocked in select
    # on another thread — the cross-thread abort ClientTransport.close guarantees.
    # A shutdown() alone is not enough: macOS does not reliably wake a parked
    # poll/select when the socket is shut down from another thread
    self._abort_receive, self._abort_send = socket.socketpair()

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
      except OSError:
        # the socket died under us — a concurrent close() aborting this wait
        # (see ClientTransport.close), or the peer tearing the channel down
        # mid-read; either way the channel is gone, which is EOF to the caller
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
