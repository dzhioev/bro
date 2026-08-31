#!/usr/bin/env python
"""broxy — the peer-side broker proxy (`broxy` console script).

A session-lifetime daemon between a session's broker clients and its one host
channel: it holds the single upstream connection to the host broker and listens
on a loopback port of its own. `BROKER_CHANNEL` points at the local address, so
every existing client (`broker` CLI, `Client.from_env`, `RunLifecycle`) works
through it unchanged. Upstream, the host sees exactly one long-lived connection
per channel — the shape its supersede-on-accept semantics were built for — while
the local side multiplexes the session's short-lived process swarm.

One event loop, no locks (the tcp adapter's concurrency model). Both sides speak
that adapter's NDJSON framing over brotocol's encoding and open with its attach
handshake. The local token authenticates rather than identifies: every local
connection attaches with the same one, since they all share the one upstream
channel.

Forwarding upstream drains inside the sending connection's own read task. By the
time a local half-close is answered, everything that connection sent has reached
the host — the guarantee `ClientTransport.close(confirm=True)` rides on.

`serve` runs one proxy and fails loudly — no restart. The upstream is the
session's own host broker: it never comes back within a session, so a lost
upstream is unrecoverable, and any other failure is a code bug to surface, not
ride through. Exit 0 means SIGTERM/SIGINT — the launcher's own teardown, the one
expected end; anything else exits 1, and the listener dies with the process, so
the session's channel disappears cleanly. The local port is ephemeral, so
`serve` publishes the address it bound through `--address-file`. `launch` owns
daemon spawn, log redirection, the readiness gate, and failure cleanup; it prints
the ready address and daemon pid for launch-policy callers. `await` remains the
standalone readiness probe.
"""

import asyncio
import contextlib
import os
import secrets
import signal
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Optional

import bro.base.args as base_args
from bro.base import log, spawn
from bro.broker.brotocol import MAX_FRAME_BYTES, Message, ProtocolError, Tag
from bro.broker.client import CHANNEL_ENV
from bro.broker.transport import Address, connect
from bro.broker.transports import tcp
from bro.broker.transports.tcp import LOCAL_HOST

__cli_name__ = 'broxy'

_LISTEN_BACKLOG = 16
# readline needs headroom over the frame cap to see a max-size frame's delimiter
_STREAM_LIMIT = MAX_FRAME_BYTES + 2
_TOKEN_BYTES = 32

MAX_ROUTES = 4096
DEFAULT_AWAIT_TIMEOUT = 10.0


async def _read_frame(reader: asyncio.StreamReader) -> Optional[bytes]:
  """read one NDJSON frame; None on EOF (a trailing partial frame is dropped).
  Raises ProtocolError on a frame over MAX_FRAME_BYTES."""
  try:
    line = await reader.readline()
  except ValueError as error:
    raise ProtocolError(f'inbound frame over {MAX_FRAME_BYTES} bytes') from error
  if not line.endswith(b'\n'):
    return None
  frame = line[:-1]
  if len(frame) > MAX_FRAME_BYTES:
    raise ProtocolError(f'inbound frame is {len(frame)} bytes, over {MAX_FRAME_BYTES}')
  return frame


class _Connection:
  """one local client connection; deliveries are write-only."""

  def __init__(self, writer: asyncio.StreamWriter):
    self.writer = writer


class Broxy:
  def __init__(
    self,
    upstream: Address,
    *,
    bind_host: str = LOCAL_HOST,
    max_routes: int = MAX_ROUTES,
  ):
    tcp.parse_address(upstream)
    if max_routes < 1:
      raise ValueError('max_routes must be positive')
    self._upstream = upstream
    self._bind_host = bind_host
    self._token = secrets.token_urlsafe(_TOKEN_BYTES)
    self._max_routes = max_routes
    self._routes: dict[str, _Connection] = {}
    self._upstream_writer: Optional[asyncio.StreamWriter] = None
    self._local_tasks: set[asyncio.Task] = set()
    self._stopped = asyncio.Event()

  def stop(self) -> None:
    """request a clean shutdown; `run` then returns 0."""
    self._stopped.set()

  async def run(self, ready: Optional[Callable[[Address], None]] = None) -> int:
    """serve until stopped or the upstream is lost.

    `ready` receives the local address once it is accepting.
    """
    try:
      upstream_reader, upstream_writer = await tcp.open_channel(self._upstream, limit=_STREAM_LIMIT)
    except (OSError, ConnectionError, TimeoutError) as error:
      log.error('broxy: cannot connect upstream %s: %s', tcp.redacted(self._upstream), error)
      return 1
    self._upstream_writer = upstream_writer
    server = await asyncio.start_server(
      self._serve_local_connection,
      host=self._bind_host,
      port=0,
      limit=_STREAM_LIMIT,
      backlog=_LISTEN_BACKLOG,
    )
    address = tcp.Endpoint(port=server.sockets[0].getsockname()[1], token=self._token).address(
      self._bind_host
    )
    log.info(
      'broxy: serving %s over upstream %s', tcp.redacted(address), tcp.redacted(self._upstream)
    )
    if ready is not None:
      ready(address)

    upstream_task = asyncio.create_task(self._read_upstream(upstream_reader))
    stopped_task = asyncio.create_task(self._stopped.wait())
    await asyncio.wait({upstream_task, stopped_task}, return_when=asyncio.FIRST_COMPLETED)
    upstream_lost = upstream_task.done()
    server.close()
    local_tasks = list(self._local_tasks)
    for task in (upstream_task, stopped_task, *local_tasks):
      task.cancel()
    await asyncio.gather(upstream_task, stopped_task, *local_tasks, return_exceptions=True)
    await server.wait_closed()
    upstream_writer.close()
    if upstream_lost:
      log.error('broxy: upstream channel lost, exiting')
      return 1
    return 0

  async def _read_upstream(self, reader: asyncio.StreamReader) -> None:
    while True:
      try:
        frame = await _read_frame(reader)
      except ProtocolError as error:
        log.error('broxy: dropping upstream channel: %s', error)
        return
      if frame is None:
        return
      try:
        message = Message.from_bytes(frame)
      except ProtocolError as error:
        log.error('broxy: dropping upstream channel on malformed frame: %s', error)
        return
      self._route_inbound(message, frame)

  def _route_inbound(self, message: Message, frame: bytes) -> None:
    connection = self._routes.get(message.quest_id)
    if connection is None:
      log.warning('broxy: dropping upstream message for unknown quest %s', message.quest_id)
      return
    if connection.writer.is_closing():
      self._remove_connection_routes(connection)
      log.warning('broxy: dropping upstream message for disconnected quest %s', message.quest_id)
      return
    connection.writer.write(frame + b'\n')
    if message.type == Tag.RESULT:
      self._routes.pop(message.quest_id, None)

  async def _serve_local_connection(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
  ) -> None:
    if await tcp.read_attach_token(reader) != self._token:
      log.warning('broxy: dropping a local connection that attached with an unknown token')
      writer.close()
      return
    if not await tcp.acknowledge_attach(writer):
      return
    async with self._local_connection(writer) as connection:
      while True:
        try:
          frame = await _read_frame(reader)
        except ProtocolError as error:
          log.warning('broxy: dropping local connection: %s', error)
          break
        if frame is None:
          break
        try:
          message = Message.from_bytes(frame)
        except ProtocolError as error:
          log.warning('broxy: dropping local connection on malformed frame: %s', error)
          break
        if message.type == Tag.REQUEST:
          self._register_route(message.quest_id, connection)
        assert self._upstream_writer is not None
        self._upstream_writer.write(frame + b'\n')
        await self._upstream_writer.drain()

  @contextlib.asynccontextmanager
  async def _local_connection(self, writer: asyncio.StreamWriter) -> AsyncIterator[_Connection]:
    connection = _Connection(writer)
    task = asyncio.current_task()
    assert task is not None
    self._local_tasks.add(task)
    try:
      yield connection
    finally:
      self._local_tasks.discard(task)
      self._remove_connection_routes(connection)
      writer.close()

  def _register_route(self, quest_id: str, connection: _Connection) -> None:
    self._routes.pop(quest_id, None)
    if len(self._routes) >= self._max_routes:
      evicted_quest = next(iter(self._routes))
      self._routes.pop(evicted_quest)
      log.warning(
        'broxy: over %d routes, dropping route for quest %s', self._max_routes, evicted_quest
      )
    self._routes[quest_id] = connection

  def _remove_connection_routes(self, connection: _Connection) -> None:
    for quest_id in [
      quest_id
      for quest_id, route_connection in self._routes.items()
      if route_connection is connection
    ]:
      self._routes.pop(quest_id)


def _serve(upstream: Optional[str], address_file: Optional[str]) -> int:
  if upstream is None:
    upstream = os.environ.get(CHANNEL_ENV)
  if upstream is None:
    log.error('no upstream channel: pass --upstream or set %s', CHANNEL_ENV)
    return 1
  try:
    broxy = Broxy(upstream)
  except ValueError as error:
    log.error('%s', error)
    return 1
  return asyncio.run(_serve_until_signalled(broxy, address_file))


async def _serve_until_signalled(broxy: Broxy, address_file: Optional[str]) -> int:
  loop = asyncio.get_running_loop()
  for signal_number in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(signal_number, broxy.stop)
  ready = None if address_file is None else _address_publisher(Path(address_file))
  return await broxy.run(ready)


def _address_publisher(path: Path) -> Callable[[Address], None]:
  """hand the launcher the ephemeral local address through a file, written whole
  so a poll never reads a half-written one."""

  def publish(address: Address) -> None:
    partial = path.with_name(f'{path.name}.partial')
    partial.write_text(address)
    partial.replace(path)

  return publish


def _await_address(path: Path, process: subprocess.Popen, timeout: float) -> Optional[Address]:
  deadline = time.monotonic() + timeout
  while True:
    if path.exists():
      return path.read_text()
    if process.poll() is not None:
      log.error('broxy exited with %d before it was listening', process.returncode)
      return None
    if time.monotonic() >= deadline:
      log.error('broxy did not report an address within %.0fs', timeout)
      return None
    time.sleep(0.05)


def _await_ready(address: str, timeout: float) -> int:
  deadline = time.monotonic() + timeout
  while True:
    try:
      connect(address).close()
      return 0
    except (ConnectionError, OSError):
      pass
    if time.monotonic() >= deadline:
      log.error('broxy %s not accepting within %.0fs', tcp.redacted(address), timeout)
      return 1
    time.sleep(0.05)


def _stop_launched_process(process: subprocess.Popen) -> None:
  process.terminate()
  try:
    process.wait(timeout=10)
  except subprocess.TimeoutExpired:
    process.kill()
    process.wait()


def _launch(log_path: str, upstream: Optional[str], timeout: float) -> int:
  if upstream is None:
    upstream = os.environ.get(CHANNEL_ENV)
  if upstream is None:
    log.error('no upstream channel: pass --upstream or set %s', CHANNEL_ENV)
    return 1

  with tempfile.TemporaryDirectory(prefix='broxy-launch-') as scratch:
    address_file = Path(scratch) / 'address'
    try:
      with open(log_path, 'a') as log_file:
        process = spawn.popen(
          ['broxy', 'serve', '--upstream', upstream, '--address-file', str(address_file)],
          stdout=log_file,
          stderr=subprocess.STDOUT,
        )
    except OSError as error:
      log.error('cannot start broxy: %s', error)
      return 1

    address = _await_address(address_file, process, timeout)
    if address is None or _await_ready(address, timeout) != 0:
      _stop_launched_process(process)
      return 1

  print(f'{address}\t{process.pid}')
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    description='peer-side broker proxy: one upstream channel, a local port for the session swarm'
  )
  subparsers = parser.add_subparsers(dest='command')

  launch_parser = subparsers.add_parser(
    'launch', help='start a detached proxy, gate on readiness, and print ADDRESS<TAB>PID'
  )
  launch_parser.add_argument(
    '--log-file', dest='log_path', required=True, help='serve stdout and stderr log file'
  )
  launch_parser.add_argument(
    '--upstream', help=f'upstream channel address (default: ${CHANNEL_ENV})'
  )
  launch_parser.add_argument(
    '--timeout',
    type=float,
    default=DEFAULT_AWAIT_TIMEOUT,
    help='seconds to wait for readiness (default: %(default)s)',
  )
  launch_parser.set_handler(_launch)

  serve_parser = subparsers.add_parser(
    'serve',
    help='run the proxy daemon (exit 0 on SIGTERM/SIGINT, 1 on a lost upstream; '
    'no restart — it fails loudly)',
  )
  serve_parser.add_argument(
    '--upstream', help=f'upstream channel address (default: ${CHANNEL_ENV})'
  )
  serve_parser.add_argument(
    '--address-file', dest='address_file', help='file to write the local address to once listening'
  )
  serve_parser.set_handler(_serve)

  await_parser = subparsers.add_parser(
    'await', help='block until the local address accepts a connection; exit 1 on timeout'
  )
  await_parser.add_argument('address', metavar='ADDRESS', help='local channel address to probe')
  await_parser.add_argument(
    '--timeout',
    type=float,
    default=DEFAULT_AWAIT_TIMEOUT,
    help='seconds to wait (default: %(default)s)',
  )
  await_parser.set_handler(_await_ready)

  return parser.dispatch(argv)
