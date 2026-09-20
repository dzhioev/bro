#!/usr/bin/env python
"""broxy — the peer-side broker proxy (`broxy` console script).

A peer-lifetime daemon between a peer's broker clients and its one host
channel: it holds the single upstream connection to the host broker and listens
on a loopback port of its own. `BROKER_CHANNEL` points at the local address, so
every client (`broker` CLI, `Client.from_env`, `RunLifecycle`) works through it.
Request routes live through their result, question routes through local EOF, and
request listeners receive unsolicited chat traffic until local EOF. Upstream, the
host sees exactly one long-lived connection per channel — the shape its
supersede-on-accept semantics were built for — while the local side multiplexes
the peer's local client processes.

One event loop, no locks (the tcp adapter's concurrency model). Both sides speak
that adapter's NDJSON framing over brotocol's encoding and open with its attach
handshake. The local token authenticates rather than identifies: every local
connection attaches with the same one, since they all share the one upstream
channel.

Forwarding upstream drains inside the sending connection's own read task. By the
time a local half-close is answered, everything that connection sent has reached
the host — the guarantee `ClientTransport.close(confirm=True)` rides on.

`serve` runs one proxy and fails loudly — no restart. The upstream is the
peer's host broker: it never comes back within that peer's lifetime, so a lost
upstream is unrecoverable, and any other failure is a code bug to surface, not
ride through. Exit 0 means SIGTERM/SIGINT — its owner's teardown, the one
expected end. Anything else exits 1, and the listener dies with the process, so
the peer's channel disappears cleanly. The local port is ephemeral, so
`serve` publishes the address it bound through `--address-file`. `launch` owns
daemon spawn, log redirection, the readiness gate, and failure cleanup; it prints
the ready address and daemon pid for launch-policy callers. `run` owns a proxy and
one command for the same span, swapping the local channel into the command's
environment and forwarding SIGTERM. `await` remains the standalone readiness
probe.
"""

import asyncio
import contextlib
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import bro.base.args as base_args
from bro.base import log, spawn
from bro.broker.brotocol import MAX_FRAME_BYTES, Message, ProtocolError, Tag
from bro.broker.environment import BROKER_CHANNEL, BROKER_UPSTREAM
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


@dataclass(frozen=True)
class _CorrelationRoute:
  id: str


@dataclass(frozen=True)
class _ListenerRoute:
  request: str
  connection: _Connection


type _Registration = _CorrelationRoute | _ListenerRoute


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
    self._listeners: dict[str, set[_Connection]] = {}
    self._registrations: dict[_Registration, None] = {}
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
    if message.type == Tag.MESSAGE and message.reply_to is not None:
      connection = self._routes.get(message.reply_to)
      if connection is not None and self._deliver(connection, frame):
        return
      log.warning('broxy: no waiting route for reply to message %s', message.reply_to)

    connection = self._routes.get(message.request_id)
    if connection is not None and self._deliver(connection, frame):
      if message.type == Tag.RESULT:
        self._remove_route(message.request_id)
      return

    listeners = list(self._listeners.get(message.request_id, ()))
    delivered = False
    for listener in listeners:
      delivered = self._deliver(listener, frame) or delivered
    if delivered:
      return
    if message.type == Tag.MESSAGE and message.reply_to is None:
      log.info('broxy: dropping message for request %s with no listener', message.request_id)
    elif message.type != Tag.MESSAGE:
      log.warning('broxy: dropping upstream message for unknown request %s', message.request_id)

  def _deliver(self, connection: _Connection, frame: bytes) -> bool:
    if connection.writer.is_closing():
      self._remove_connection_routes(connection)
      return False
    connection.writer.write(frame + b'\n')
    return True

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
          self._register_route(message.request_id, connection)
        elif message.type == Tag.MESSAGE and message.id is not None:
          self._register_route(message.id, connection)
        elif message.type == Tag.MARK and message.payload['transition'] == 'listening':
          self._register_listener(message.request_id, connection)
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

  def _register_route(self, correlation_id: str, connection: _Connection) -> None:
    registration = _CorrelationRoute(correlation_id)
    self._remove_registration(registration)
    self._make_room()
    self._routes[correlation_id] = connection
    self._registrations[registration] = None

  def _register_listener(self, request_id: str, connection: _Connection) -> None:
    registration = _ListenerRoute(request_id, connection)
    if registration in self._registrations:
      return
    self._make_room()
    self._listeners.setdefault(request_id, set()).add(connection)
    self._registrations[registration] = None

  def _make_room(self) -> None:
    if len(self._registrations) < self._max_routes:
      return
    oldest = next(iter(self._registrations))
    self._remove_registration(oldest)
    correlation = isinstance(oldest, _CorrelationRoute)
    name = oldest.id if correlation else oldest.request
    kind = 'correlation route' if correlation else 'listener'
    log.warning(
      'broxy: at %d-route bound, dropping oldest %s for %s',
      self._max_routes,
      kind,
      name,
    )

  def _remove_route(self, correlation_id: str) -> None:
    self._remove_registration(_CorrelationRoute(correlation_id))

  def _remove_registration(self, registration: _Registration) -> None:
    self._registrations.pop(registration, None)
    if isinstance(registration, _CorrelationRoute):
      self._routes.pop(registration.id, None)
      return
    listeners = self._listeners.get(registration.request)
    if listeners is None:
      return
    listeners.discard(registration.connection)
    if len(listeners) == 0:
      self._listeners.pop(registration.request)

  def _remove_connection_routes(self, connection: _Connection) -> None:
    for registration in [
      registration
      for registration in self._registrations
      if (
        isinstance(registration, _CorrelationRoute)
        and self._routes.get(registration.id) is connection
      )
      or (isinstance(registration, _ListenerRoute) and registration.connection is connection)
    ]:
      self._remove_registration(registration)


def _serve(upstream: Optional[str], address_file: Optional[str]) -> int:
  if upstream is None:
    upstream = os.environ.get(BROKER_CHANNEL)
  if upstream is None:
    log.error('no upstream channel: pass --upstream or set %s', BROKER_CHANNEL)
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
  if process.poll() is not None:
    return
  process.terminate()
  try:
    process.wait(timeout=10)
  except subprocess.TimeoutExpired:
    process.kill()
    process.wait()


def _launch(log_path: str, upstream: Optional[str], timeout: float) -> int:
  if upstream is None:
    upstream = os.environ.get(BROKER_CHANNEL)
  if upstream is None:
    log.error('no upstream channel: pass --upstream or set %s', BROKER_CHANNEL)
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


@contextlib.contextmanager
def _forward_sigterm(process: subprocess.Popen) -> Generator[None]:
  def forward(_number, _frame) -> None:
    if process.poll() is None:
      process.terminate()

  previous = signal.signal(signal.SIGTERM, forward)
  try:
    yield
  finally:
    signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def _command_proxy(upstream: str, log_path: Optional[str]) -> Generator[Optional[Address]]:
  with contextlib.ExitStack() as resources:
    scratch = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='broxy-run-')))
    address_file = scratch / 'address'
    if log_path is None:
      output = sys.stderr
    else:
      try:
        output = resources.enter_context(open(log_path, 'a'))
      except OSError as error:
        log.error('cannot open broxy log: %s', error)
        yield None
        return
    try:
      process = spawn.popen(
        ['broxy', 'serve', '--upstream', upstream, '--address-file', str(address_file)],
        stdout=output,
        stderr=subprocess.STDOUT,
      )
    except OSError as error:
      log.error('cannot start broxy: %s', error)
      yield None
      return
    address = _await_address(address_file, process, DEFAULT_AWAIT_TIMEOUT)
    if address is None or _await_ready(address, DEFAULT_AWAIT_TIMEOUT) != 0:
      _stop_launched_process(process)
      yield None
      return
    try:
      yield address
    finally:
      _stop_launched_process(process)


def _run(argv: list[str], log_path: Optional[str]) -> int:
  command = argv[1:] if argv[:1] == ['--'] else argv
  upstream = os.environ.get(BROKER_UPSTREAM)
  if upstream is None:
    log.error('no upstream channel: set %s', BROKER_UPSTREAM)
    return 1
  if len(command) == 0:
    log.error('no command: pass one after --')
    return 1
  with _command_proxy(upstream, log_path) as address:
    if address is None:
      return 1
    environment = dict(os.environ)
    environment.pop(BROKER_UPSTREAM, None)
    environment[BROKER_CHANNEL] = address
    try:
      process = subprocess.Popen(command, env=environment)
    except OSError as error:
      log.error('cannot start command: %s', error)
      return 1
    with _forward_sigterm(process):
      status = process.wait()
  return status if status >= 0 else 128 - status


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    description="peer-side broker proxy: one upstream channel, a local port for the peer's clients"
  )
  subparsers = parser.add_subparsers(dest='command')

  launch_parser = subparsers.add_parser(
    'launch', help='start a detached proxy, gate on readiness, and print ADDRESS<TAB>PID'
  )
  launch_parser.add_argument(
    '--log-file', dest='log_path', required=True, help='serve stdout and stderr log file'
  )
  launch_parser.add_argument(
    '--upstream', help=f'upstream channel address (default: ${BROKER_CHANNEL})'
  )
  launch_parser.add_argument(
    '--timeout',
    type=float,
    default=DEFAULT_AWAIT_TIMEOUT,
    help='seconds to wait for readiness (default: %(default)s)',
  )
  launch_parser.set_handler(_launch)

  run_parser = subparsers.add_parser(
    'run', help=f'run a command through a proxy attached to ${BROKER_UPSTREAM}'
  )
  run_parser.add_argument('--log-file', dest='log_path', help='proxy log file (default: stderr)')
  run_parser.add_argument(
    'argv', nargs=base_args.REMAINDER, metavar='COMMAND', help='command and arguments after --'
  )
  run_parser.set_handler(_run)

  serve_parser = subparsers.add_parser(
    'serve',
    help='run the proxy daemon (exit 0 on SIGTERM/SIGINT, 1 on a lost upstream; '
    'no restart — it fails loudly)',
  )
  serve_parser.add_argument(
    '--upstream', help=f'upstream channel address (default: ${BROKER_CHANNEL})'
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
