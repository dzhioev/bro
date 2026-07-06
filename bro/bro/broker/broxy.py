#!/usr/bin/env python
"""broxy — the peer-side broker proxy (`broxy` console script).

A session-lifetime daemon between a session's broker clients and its one host
channel: it holds the single upstream connection to the host broker and listens
on a local unix socket. `BROKER_CHANNEL` points at the local socket, so every
existing client (`broker` CLI, `Client.from_env`, `BroChannel`) works through it
unchanged. Upstream, the host sees exactly one long-lived connection per channel
— the shape its supersede-on-accept semantics were built for — while the local
side multiplexes the session's short-lived process swarm.

One event loop, no locks (the unix adapter's concurrency model). Both sides
speak that adapter's NDJSON framing over brotocol's encoding; v1 is unix-only on
both sides, like `transport.connect`.

Sticky routing: outbound frames are deframed to learn which local connection
sent which request id; correlated inbound is routed to exactly that connection.
A route survives interim messages and retires on the terminal — any correlated
type but `started`, mirroring `Client.call`.

Mailbox: a correlated message whose waiter connection is gone lands in a bounded
in-memory mailbox keyed by request id, all messages kept in order (so a later
claim replays `started` before the terminal). Over the byte bound, whole oldest
entries are dropped — their claim then fails fast instead of replaying a gapped
sequence.

Claim: the local-only `claim{id}` control message (`Tag.CLAIM`, never forwarded
upstream) collects or re-awaits a request's messages. The claim acts as a
stand-in request: buffered messages are replayed — and future ones delivered —
re-tagged to correlate to the claim message itself, so `Client.call('claim',
{'id': ...})` rides an interim `started` and returns the terminal exactly like
the original call did. An unknown or already-consumed id gets an immediate
`reply{error}` (fail fast, not hang). The wait is a lock: while the current
waiter's connection is alive, a competing claim gets an immediate `reply{error}`
rather than stealing the route; only a detached route — the waiter exited or was
killed — is claimable, which is the recovery path.

Check: the local-only `check{id}` sibling (`Tag.CHECK`) — a non-consuming,
non-superseding peek, always answered immediately. A buffered terminal replays
copies of the request's messages re-tagged to the check id (the route is left
untouched, so a later check or claim still finds it); anything still in flight
gets `reply{state: 'pending'}` (plus the trail id when a buffered `started`
carries it) with any live waiter's route undisturbed — the property claim cannot
give a poller; an id the broxy doesn't know gets `reply{state: 'unknown'}`.

Delivery to a local waiter is write-only, no drain: frames per request are few
and model-bounded, and a drain there would let one stalled local reader stall
routing for every other connection. Forwarding upstream does drain, inside the
sending connection's own read task — so by the time a local half-close is
answered, everything that connection sent has reached the host (the guarantee
`ClientTransport.close(confirm=True)` rides on).

`serve` runs one proxy and fails loudly — no restart. The upstream is the
session's own host broker over a local unix socket: it never comes back within
a session, so a lost upstream is unrecoverable, and any other failure is a code
bug to surface, not ride through (the in-memory mailbox dies with the process
either way — durability is deliberately out of scope). Exit 0 means
SIGTERM/SIGINT — the launcher's own teardown, the one expected end; anything
else exits 1, the socket unlinked, so the session's channel disappears cleanly.
`await` blocks until the local socket accepts a connection: the readiness gate
a launcher rewrites `BROKER_CHANNEL` behind — when it fails, the session gets
no channel at all.
"""

import asyncio
import os
import signal
import socket
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import base.args
from base import log
from broker.brotocol import MAX_FRAME_BYTES, Message, ProtocolError, Tag
from broker.client import CHANNEL_ENV

__cli_name__ = 'broxy'

_LISTEN_BACKLOG = 16
# readline needs headroom over the frame cap to see a max-size frame's delimiter
_STREAM_LIMIT = MAX_FRAME_BYTES + 2

MAILBOX_MAX_BYTES = 8 << 20  # buffered undelivered messages, totalled across requests
MAX_ROUTES = 4096  # request ids remembered for correlation (every outbound send mints one)

DEFAULT_AWAIT_TIMEOUT = 10.0


async def _read_frame(reader: asyncio.StreamReader) -> Optional[bytes]:
  """read one NDJSON frame; None on EOF (a trailing partial frame is dropped).
  Raises ProtocolError on a frame over MAX_FRAME_BYTES."""
  try:
    line = await reader.readline()
  except ValueError as e:  # the stream limit tripped mid-line
    raise ProtocolError(f'inbound frame over {MAX_FRAME_BYTES} bytes') from e
  if not line.endswith(b'\n'):  # empty or partial: EOF either way
    return None
  frame = line[:-1]
  if len(frame) > MAX_FRAME_BYTES:
    raise ProtocolError(f'inbound frame is {len(frame)} bytes, over {MAX_FRAME_BYTES}')
  return frame


class _Connection:
  """one local client connection; deliveries are write-only (see module docstring)."""

  def __init__(self, writer: asyncio.StreamWriter):
    self.writer = writer


@dataclass
class _Route:
  """correlation state for one outbound request id."""

  waiter: Optional[_Connection]  # None once the sending (or last claiming) connection is gone
  reply_to: str  # correlation id stamped on delivery: the request id, or the latest claim's id
  buffered: list[Message] = field(default_factory=list)
  buffered_bytes: int = 0
  terminal_buffered: bool = False


class Broxy:
  def __init__(
    self,
    upstream: str,
    socket_path: Path,
    *,
    mailbox_bytes: int = MAILBOX_MAX_BYTES,
    max_routes: int = MAX_ROUTES,
  ):
    scheme, separator, upstream_path = upstream.partition(':')
    if separator == '' or scheme != 'unix':
      raise ValueError(f'unsupported broxy upstream address {upstream!r} (unix only)')
    self._upstream_path = upstream_path
    self._socket_path = socket_path
    self._mailbox_bytes = mailbox_bytes
    self._max_routes = max_routes
    self._routes: dict[str, _Route] = {}  # insertion order = mint order, oldest first
    self._buffered_total = 0
    self._upstream_writer: Optional[asyncio.StreamWriter] = None
    self._local_tasks: set[asyncio.Task] = set()
    self._stopped = asyncio.Event()

  def stop(self) -> None:
    """request a clean shutdown; `run` then returns 0."""
    self._stopped.set()

  async def run(self) -> int:
    try:
      upstream_reader, upstream_writer = await asyncio.open_unix_connection(
        self._upstream_path, limit=_STREAM_LIMIT
      )
    except OSError as e:
      log.error('broxy: cannot connect upstream %s: %s', self._upstream_path, e)
      return 1
    self._upstream_writer = upstream_writer
    self._socket_path.parent.mkdir(parents=True, exist_ok=True)
    self._socket_path.unlink(missing_ok=True)  # stale socket from a crashed prior run
    server = await asyncio.start_unix_server(
      self._serve_local_connection,
      path=str(self._socket_path),
      limit=_STREAM_LIMIT,
      backlog=_LISTEN_BACKLOG,
    )
    os.chmod(self._socket_path, 0o600)
    log.info('broxy: serving %s over upstream %s', self._socket_path, self._upstream_path)

    upstream_task = asyncio.create_task(self._read_upstream(upstream_reader))
    stopped_task = asyncio.create_task(self._stopped.wait())
    await asyncio.wait({upstream_task, stopped_task}, return_when=asyncio.FIRST_COMPLETED)
    upstream_lost = upstream_task.done()
    server.close()  # stop accepting before tearing the handlers down
    local_tasks = list(self._local_tasks)
    for task in (upstream_task, stopped_task, *local_tasks):
      task.cancel()
    await asyncio.gather(upstream_task, stopped_task, *local_tasks, return_exceptions=True)
    await server.wait_closed()
    upstream_writer.close()
    self._socket_path.unlink(missing_ok=True)
    if upstream_lost:
      log.error('broxy: upstream channel lost, exiting')
      return 1
    return 0

  # --- upstream → local ------------------------------------------------------

  async def _read_upstream(self, reader: asyncio.StreamReader) -> None:
    while True:
      try:
        frame = await _read_frame(reader)
      except ProtocolError as e:
        log.error('broxy: dropping upstream channel: %s', e)
        return
      if frame is None:
        return
      try:
        message = Message.from_bytes(frame)
      except ProtocolError as e:
        log.error('broxy: dropping upstream channel on malformed frame: %s', e)
        return
      self._route_inbound(message, frame)

  def _route_inbound(self, message: Message, frame: bytes) -> None:
    if message.in_reply_to is None:
      # v1 has no local recipient for an unsolicited host message
      log.warning('broxy: dropping uncorrelated upstream message %s (%s)', message.id, message.type)
      return
    route = self._routes.get(message.in_reply_to)
    if route is None:  # e.g. the route was evicted, or its mailbox entry dropped
      log.warning('broxy: dropping upstream message for unknown request %s', message.in_reply_to)
      return
    terminal = message.type != Tag.STARTED  # the terminal rule, mirroring Client.call
    waiter = route.waiter
    if waiter is not None and waiter.writer.is_closing():
      route.waiter = None
      waiter = None
    if waiter is None:
      route.buffered.append(message)
      route.buffered_bytes += len(frame)
      self._buffered_total += len(frame)
      if terminal:
        route.terminal_buffered = True
      self._enforce_mailbox_bound()
      return
    self._deliver(waiter, message, route.reply_to)
    if terminal:
      self._drop_route(message.in_reply_to)

  def _deliver(self, connection: _Connection, message: Message, reply_to: str) -> None:
    if message.in_reply_to != reply_to:
      message = replace(message, in_reply_to=reply_to)
    connection.writer.write(message.to_bytes() + b'\n')

  # --- local → upstream ------------------------------------------------------

  async def _serve_local_connection(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
  ) -> None:
    connection = _Connection(writer)
    task = asyncio.current_task()
    assert task is not None
    self._local_tasks.add(task)
    try:
      while True:
        try:
          frame = await _read_frame(reader)
        except ProtocolError as e:
          log.warning('broxy: dropping local connection: %s', e)
          break
        if frame is None:
          break
        try:
          message = Message.from_bytes(frame)
        except ProtocolError as e:
          log.warning('broxy: dropping local connection on malformed frame: %s', e)
          break
        if message.type == Tag.CLAIM:
          self._handle_claim(connection, message)
          continue
        if message.type == Tag.CHECK:
          self._handle_check(connection, message)
          continue
        self._register_route(message.id, connection)
        assert self._upstream_writer is not None
        self._upstream_writer.write(frame + b'\n')
        await self._upstream_writer.drain()
    finally:
      self._local_tasks.discard(task)
      # detach before closing back: when the peer's half-close handshake returns,
      # later correlated messages are already guaranteed to buffer for a claim
      for route in self._routes.values():
        if route.waiter is connection:
          route.waiter = None
      writer.close()

  def _handle_claim(self, connection: _Connection, claim: Message) -> None:
    claimed_id = claim.payload.get('id')
    if not isinstance(claimed_id, str):
      error = Message(
        type=Tag.REPLY,
        payload={'error': "claim payload must carry a string 'id'"},
        in_reply_to=claim.id,
      )
      self._deliver(connection, error, claim.id)
      return
    route = self._routes.get(claimed_id)
    if route is None:
      error = Message(
        type=Tag.REPLY,
        payload={'error': f'unknown or already-consumed request id {claimed_id}'},
        in_reply_to=claim.id,
      )
      self._deliver(connection, error, claim.id)
      return
    waiter = route.waiter
    if waiter is not None and waiter.writer.is_closing():
      route.waiter = None
      waiter = None
    if waiter is not None and waiter is not connection:
      # the wait is a lock: a live waiter keeps its route, the newcomer fails fast
      # (a killed waiter's connection is gone, so reclaiming it still works)
      error = Message(
        type=Tag.REPLY,
        payload={'error': f'request {claimed_id} is already being awaited'},
        in_reply_to=claim.id,
      )
      self._deliver(connection, error, claim.id)
      return
    route.waiter = connection
    route.reply_to = claim.id
    buffered = route.buffered
    self._buffered_total -= route.buffered_bytes
    route.buffered = []
    route.buffered_bytes = 0
    for message in buffered:
      self._deliver(connection, message, claim.id)
    if route.terminal_buffered:
      self._drop_route(claimed_id)  # replayed through the terminal: consumed

  def _handle_check(self, connection: _Connection, check: Message) -> None:
    checked_id = check.payload.get('id')
    if not isinstance(checked_id, str):
      error = Message(
        type=Tag.REPLY,
        payload={'error': "check payload must carry a string 'id'"},
        in_reply_to=check.id,
      )
      self._deliver(connection, error, check.id)
      return
    route = self._routes.get(checked_id)
    if route is None:
      state = Message(type=Tag.REPLY, payload={'state': 'unknown'}, in_reply_to=check.id)
      self._deliver(connection, state, check.id)
      return
    if route.terminal_buffered:
      # replay copies re-tagged to the check; the route (waiter, reply_to, buffer)
      # is untouched, so the peek consumes nothing and a later check/claim still works
      for message in route.buffered:
        self._deliver(connection, message, check.id)
      return
    payload: dict = {'state': 'pending'}
    for message in route.buffered:
      if message.type == Tag.STARTED and message.payload.get('trail_id') is not None:
        payload['trail_id'] = message.payload['trail_id']
        break
    state = Message(type=Tag.REPLY, payload=payload, in_reply_to=check.id)
    self._deliver(connection, state, check.id)

  # --- route table -----------------------------------------------------------

  def _register_route(self, request_id: str, connection: _Connection) -> None:
    self._drop_route(
      request_id
    )  # a client-reused id resets its route (ULIDs make this theoretical)
    if len(self._routes) >= self._max_routes:
      self._evict_route()
    self._routes[request_id] = _Route(waiter=connection, reply_to=request_id)

  def _drop_route(self, request_id: str) -> None:
    route = self._routes.pop(request_id, None)
    if route is not None:
      self._buffered_total -= route.buffered_bytes

  def _evict_route(self) -> None:
    """make room: drop the oldest detached route, preferring one with nothing buffered."""
    fallback: Optional[str] = None
    for request_id, route in self._routes.items():
      if route.waiter is not None:
        continue
      if route.buffered_bytes == 0:
        self._drop_route(request_id)
        return
      if fallback is None:
        fallback = request_id
    if fallback is not None:
      log.warning('broxy: over %d routes, dropping buffered request %s', self._max_routes, fallback)
      self._drop_route(fallback)
    # otherwise every route has a live waiter: exceed the cap rather than break one

  def _enforce_mailbox_bound(self) -> None:
    while self._buffered_total > self._mailbox_bytes:
      request_id = next(id for id, route in self._routes.items() if route.buffered_bytes > 0)
      log.warning(
        'broxy: mailbox over %d bytes, dropping buffered request %s',
        self._mailbox_bytes,
        request_id,
      )
      self._drop_route(request_id)


# --- CLI ----------------------------------------------------------------------


def _serve(socket_path: str, upstream: Optional[str]) -> int:
  if upstream is None:
    upstream = os.environ.get(CHANNEL_ENV)
  if upstream is None:
    log.error('no upstream channel: pass --upstream or set %s', CHANNEL_ENV)
    return 1
  try:
    broxy = Broxy(upstream, Path(socket_path))
  except ValueError as e:
    log.error('%s', e)
    return 1
  return asyncio.run(_serve_until_signalled(broxy))


async def _serve_until_signalled(broxy: Broxy) -> int:
  loop = asyncio.get_running_loop()
  for signal_number in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(signal_number, broxy.stop)
  return await broxy.run()


def _await_ready(socket_path: str, timeout: float) -> int:
  deadline = time.monotonic() + timeout
  while True:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
      probe.connect(socket_path)
      return 0
    except OSError:
      pass
    finally:
      probe.close()
    if time.monotonic() >= deadline:
      log.error('broxy socket %s not accepting within %.0fs', socket_path, timeout)
      return 1
    time.sleep(0.05)


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(
    description='peer-side broker proxy: one upstream channel, a local socket for the session swarm'
  )
  subparsers = parser.add_subparsers(dest='command')

  serve_parser = subparsers.add_parser(
    'serve',
    help='run the proxy daemon (exit 0 on SIGTERM/SIGINT, 1 on a lost upstream; '
    'no restart — it fails loudly)',
  )
  serve_parser.add_argument('socket_path', metavar='SOCKET', help='local unix socket to listen on')
  serve_parser.add_argument(
    '--upstream', help=f'upstream channel address (default: ${CHANNEL_ENV})'
  )
  serve_parser.set_handler(_serve)

  await_parser = subparsers.add_parser(
    'await', help='block until the local socket accepts a connection; exit 1 on timeout'
  )
  await_parser.add_argument('socket_path', metavar='SOCKET', help='local unix socket to probe')
  await_parser.add_argument(
    '--timeout',
    type=float,
    default=DEFAULT_AWAIT_TIMEOUT,
    help='seconds to wait (default: %(default)s)',
  )
  await_parser.set_handler(_await_ready)

  return parser.dispatch(argv)
