"""Dispatcher — the logic layer: exchanges, the routing rules, kind handlers.

The `Runtime` (mechanism) reports raw, symmetric lifecycle up to this `Dispatcher`
(logic), which owns everything protocol: the live exchanges — per exchange the
requesting peer, the request id, and at most one worker channel — the kind-handler
registry, the delivery observers, the three routing rules, and the synthesis of
`result{failed}`. It runs only inside `Runtime` callbacks, all on the one event loop,
so it is a plain synchronous object with no lock.

Two invariants carry the design:

- **Exactly one result per exchange.** Delivering a result closes the exchange and a
  closed exchange is forgotten, so any later message naming it falls to rule 3 and is
  dropped. Synthesis consults the same table, so whichever of the worker's own result /
  exit / timeout is processed first wins — closing the result-vs-exit and
  timeout-vs-result double-terminal races.
- **`failed` is the only outcome the host originates on a worker's behalf.** A worker's
  `ok` is always its own; the host synthesizes `result{failed}` only when the worker
  ends without one — an `on_exit` without it (`reason: 'exit'`), an `on_timeout`
  (`reason: 'timeout'`, after the Runtime already killed the peer), an `on_gone`
  without one (`reason: 'disconnected'`, an expected peer's channel ended), or a
  `spawn` whose launch raised (`reason: 'launch'`, before any peer existed).

The root is a uniform peer with one twist: `run(root)` opens the session's own
exchange for it, host-anchored — the requester is this process, not a peer — so the
root announces its run lifecycle like any worker. A host-anchored delivery reaches the
delivery observers with `target=None` and nobody else, and when the root exits without
a result the exchange closes silently: the host reads the exit code itself, and the
exit ends the session.
"""

import asyncio
import contextlib
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Optional, Protocol

from bro.base import log
from bro.base.lulid import lulid
from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.runtime import Peer, Runtime
from bro.broker.spawn import LaunchSpec, Spawner
from bro.broker.transport import Provisioned, ServerTransport

# request-lifecycle bound for a spawned worker (LLM children run for minutes)
DEFAULT_TIMEOUT = 600.0

# the reserved kind: answers ok with its own arguments echoed back
PING = 'ping'

# a handler receives the Dispatcher as its `context` and drives the routing primitives.
RequestHandler = Callable[['Dispatcher', Peer, Message], None]

# a delivery observer taps correlated deliveries as (source, target, delivered message);
# source is None when no worker ever existed (a launch failure), and target is None on
# a host-anchored exchange (the root's — this process is the requester).
DeliveryObserver = Callable[[Optional[Peer], Optional[Peer], Message], None]


class RuntimeCommands(Protocol):
  """the mechanism-layer commands the Dispatcher issues; the real `Runtime` satisfies it."""

  async def spawn(self, launch: LaunchSpec, *, timeout: Optional[float], exchange: str) -> Peer: ...
  async def expect(self, *, timeout: Optional[float]) -> Provisioned: ...
  def send(self, peer: Peer, message: Message) -> None: ...
  def kill(self, peer: Peer) -> None: ...
  def forget(self, peer: Peer) -> None: ...
  async def serve(self) -> None: ...
  async def stop(self) -> None: ...


@dataclass
class Exchange:
  """one live exchange: who awaits its result, and the worker channel answering it
  (None while the launch is still resolving)."""

  requester: Optional[Peer]  # None: host-anchored — this process is the requester
  worker: Optional[Peer] = None


class Dispatcher:
  def __init__(self, *, default_timeout: float = DEFAULT_TIMEOUT):
    self._runtime: Optional[RuntimeCommands] = None
    self._default_timeout = default_timeout
    self.exchanges: dict[str, Exchange] = {}  # live exchanges by request id
    self.workers: dict[Peer, str] = {}  # worker channel -> the exchange it answers
    self._handlers: dict[str, RequestHandler] = {}
    self._delivery_observers: list[DeliveryObserver] = []
    self._active: Optional[Message] = None  # the request currently being handled (for reply/spawn)
    self._root: Optional[Peer] = None
    self._root_exit: Optional[asyncio.Future[int]] = None

  def bind(self, runtime: RuntimeCommands) -> None:
    """wire the Dispatcher to the Runtime it commands (the facade breaks the mutual cycle:
    the Runtime needs this Dispatcher as its listener, this needs the Runtime for commands)."""
    self._runtime = runtime

  @property
  def runtime(self) -> RuntimeCommands:
    if self._runtime is None:
      raise RuntimeError('dispatcher used before bind()')
    return self._runtime

  def on(self, kind: str, handler: RequestHandler) -> None:
    self._handlers[kind] = handler

  def add_delivery_observer(self, observer: DeliveryObserver) -> None:
    """register a tap on the correlated deliveries that bypass handlers — rule-1
    forwarding and synthesized `failed` — so a consumer sees worker lifecycle (trail
    ids, outcomes) without sitting in the message path. Handler-driven `reply`, the
    dispatcher's own denials, and rule-3 refusals are not deliveries it reports."""
    self._delivery_observers.append(observer)

  @property
  def root(self) -> Optional[Peer]:
    """the root peer's channel once `run()` has spawned it — the key for root-aware policy checks."""
    return self._root

  # --- routing primitives (used by the rules; a handler drives the same set) ----

  def deliver(self, peer: Peer, message: Message) -> None:
    self.runtime.send(peer, message)

  def reply(self, peer: Peer, payload: dict) -> None:
    """answer the in-flight request with its result; `payload` is the result payload
    (`{outcome, value?, error?, detail?}`)."""
    self.deliver(peer, Message(type=Tag.RESULT, payload=payload, request=self._request_id()))

  def refuse(self, peer: Peer, message: Message) -> None:
    """drop an unroutable progress/result with a log line (rule 3)."""
    log.warning(f'broker dispatcher: refused {message.type!r} from peer {peer} (no live exchange)')

  def spawn(self, launch: LaunchSpec, requester: Peer, *, timeout: Optional[float] = None) -> None:
    """spawn `launch` as the worker answering the in-flight request. The exchange opens
    here — held against id collisions from this point — and the worker channel binds
    when the launch resolves, before the launched process can have connected and sent
    a frame. A launch failure closes the exchange with `result{failed, reason:
    'launch'}` back to `requester` instead."""
    request_id = self._request_id()
    self.exchanges[request_id] = Exchange(requester=requester)
    effective_timeout = timeout if timeout is not None else self._default_timeout
    task = asyncio.ensure_future(
      self.runtime.spawn(launch, timeout=effective_timeout, exchange=request_id)
    )

    def _launched(finished: asyncio.Task) -> None:
      if finished.cancelled():
        return
      error = finished.exception()
      if error is not None:
        log.warning(f'broker dispatcher: spawn failed: {error!r}')
        self._fail(request_id, None, error=str(error), detail={'reason': 'launch'})
        return
      self._bind_worker(finished.result(), request_id)

    task.add_done_callback(_launched)

  def expect(
    self,
    requester: Peer,
    *,
    timeout: Optional[float],
    ready: Callable[[Provisioned], None],
  ) -> None:
    """register an expected external worker for the in-flight request — `spawn` for a
    peer someone else launches. Once the channel is provisioned and bound, `ready`
    receives it (on the loop) so the handler can publish the endpoint to whatever
    launches the peer; a provisioning failure closes the exchange with `result{failed,
    reason: 'launch'}`. `timeout` bounds the whole expectation; None leaves it
    unbounded — an external peer's arrival is paced by its launcher, not by this
    host."""
    request_id = self._request_id()
    self.exchanges[request_id] = Exchange(requester=requester)
    task = asyncio.ensure_future(self.runtime.expect(timeout=timeout))

    def _provisioned(finished: asyncio.Task) -> None:
      if finished.cancelled():
        return
      error = finished.exception()
      if error is not None:
        log.warning(f'broker dispatcher: expect failed: {error!r}')
        self._fail(request_id, None, error=str(error), detail={'reason': 'launch'})
        return
      provisioned = finished.result()
      self._bind_worker(provisioned.channel, request_id)
      ready(provisioned)

    task.add_done_callback(_provisioned)

  @contextlib.contextmanager
  def _as_active(self, message: Message) -> Generator[None]:
    previous = self._active
    self._active = message
    try:
      yield
    finally:
      self._active = previous

  def invoke(self, peer: Peer, message: Message) -> None:
    """dispatch a request to its kind's handler (rule 2), exposing the request to
    `reply` / `spawn` as the in-flight one for the duration of the call."""
    handler = self._handlers[message.kind]
    with self._as_active(message):
      handler(self, peer, message)

  # --- Runtime listener (all on the loop) ---------------------------------

  def on_connect(self, peer: Peer) -> None:
    del peer  # exchanges are registered at spawn time, so birth needs no dispatcher action

  def on_message(self, peer: Peer, message: Message) -> None:
    if message.type == Tag.REQUEST:
      self._on_request(peer, message)
    else:
      self._on_answer(peer, message)

  def on_exit(self, peer: Peer, code: int, output: str) -> None:
    request_id = self.workers.get(peer)
    if request_id is not None:
      self._fail(
        request_id, peer, detail={'reason': 'exit', 'exit_code': code, 'output_tail': output}
      )
    self._cleanup(peer)
    if peer == self._root and self._root_exit is not None and not self._root_exit.done():
      self._root_exit.set_result(code)

  def on_timeout(self, peer: Peer) -> None:
    # the Runtime already killed the peer; the following on_exit finds the exchange
    # closed and synthesizes nothing.
    request_id = self.workers.get(peer)
    if request_id is not None:
      self._fail(request_id, peer, detail={'reason': 'timeout'})

  def on_gone(self, peer: Peer) -> None:
    # an expected worker's channel ended — its `on_exit`: every frame it wrote was
    # already delivered, so a live exchange got no result from it.
    request_id = self.workers.get(peer)
    if request_id is not None:
      self._fail(request_id, peer, detail={'reason': 'disconnected'})
    self._cleanup(peer)

  # --- the three routing rules --------------------------------------------

  def _on_request(self, peer: Peer, message: Message) -> None:
    """rule 2: a request goes to the handler registered for its kind; no handler —
    or an id colliding with a live exchange (uniqueness rides on entropy, so a
    collision is rejected rather than coped with) — means `result{denied}`."""
    if message.exchange in self.exchanges:
      self._deny(peer, message.exchange, f'request id {message.exchange} already names a live exchange')  # fmt: skip
      return
    if message.kind not in self._handlers:
      self._deny(peer, message.exchange, f'unknown kind {message.kind!r}')
      return
    self.invoke(peer, message)

  def _on_answer(self, peer: Peer, message: Message) -> None:
    """rule 1: a progress/result naming a live exchange, arriving on that exchange's
    own worker channel, is forwarded to the requester unchanged — the sender must *be*
    the worker, so learning another exchange's id gains a peer nothing. Anything else
    is rule 3: dropped and logged."""
    exchange = self.exchanges.get(message.request) if message.request is not None else None
    if exchange is None or exchange.worker != peer:
      self.refuse(peer, message)
      return
    self._deliver_observed(peer, exchange.requester, message)
    if message.type == Tag.RESULT:
      self._close(message.exchange)

  # --- facade support -----------------------------------------------------

  async def run(self, root: LaunchSpec) -> int:
    """serve, then spawn the root as the uniform worker of a freshly minted
    host-anchored exchange (no request-lifecycle timeout), await its exit, tear down
    any outstanding children, and return the root's exit code."""
    self._root_exit = asyncio.get_running_loop().create_future()
    serve_task = asyncio.ensure_future(self.runtime.serve())
    await asyncio.sleep(0)  # let serve install the sink before the root connects
    exchange = lulid()
    self.exchanges[exchange] = Exchange(requester=None)
    self._root = await self.runtime.spawn(root, timeout=None, exchange=exchange)
    self._bind_worker(self._root, exchange)
    try:
      return await self._root_exit
    finally:
      await self.runtime.stop()
      serve_task.cancel()
      await asyncio.gather(serve_task, return_exceptions=True)

  def stop(self) -> None:
    """unblock a running `run()` from the loop thread; its teardown does the actual stop."""
    if self._root_exit is not None and not self._root_exit.done():
      self._root_exit.set_result(0)

  # --- internals ----------------------------------------------------------

  def _request_id(self) -> str:
    if self._active is None:
      raise RuntimeError('reply()/spawn() called outside a request handler')
    return self._active.exchange

  def _bind_worker(self, peer: Peer, request_id: str) -> None:
    self.exchanges[request_id].worker = peer
    self.workers[peer] = request_id

  def _deny(self, peer: Peer, request_id: str, error: str) -> None:
    log.warning(f'broker dispatcher: denied request {request_id}: {error}')
    self.deliver(peer, brotocol.result(request_id, 'denied', error=error))

  def _deliver_observed(
    self, source: Optional[Peer], target: Optional[Peer], message: Message
  ) -> None:
    """deliver + fire the delivery tap — the seam for the correlated deliveries
    handlers never see (rule-1 forwarding and synthesized `failed`). A host-anchored
    exchange has no peer to deliver to: its messages reach only the observers."""
    if target is not None:
      self.deliver(target, message)
    for observer in self._delivery_observers:
      observer(source, target, message)

  def _fail(
    self,
    request_id: str,
    source: Optional[Peer],
    *,
    error: Optional[str] = None,
    detail: dict,
  ) -> None:
    """close a live exchange with a synthesized `result{failed}` to its requester. A
    host-anchored exchange closes silently: the root's death is its exit code, which
    this process reads itself."""
    exchange = self.exchanges.get(request_id)
    if exchange is None:
      return
    self._close(request_id)
    if exchange.requester is None:
      return
    self._deliver_observed(
      source, exchange.requester, brotocol.result(request_id, 'failed', error=error, detail=detail)
    )

  def _close(self, request_id: str) -> None:
    exchange = self.exchanges.pop(request_id, None)
    if exchange is not None and exchange.worker is not None:
      self.workers.pop(exchange.worker, None)

  def _cleanup(self, peer: Peer) -> None:
    """drop the peer's exchange (when one is still live) and the Runtime's bookkeeping."""
    request_id = self.workers.pop(peer, None)
    if request_id is not None:
      self.exchanges.pop(request_id, None)
    self.runtime.forget(peer)


class Broker:
  """the thin facade ride constructs: injects the two ports, exposes `on` / `run` / `stop`."""

  def __init__(
    self, transport: ServerTransport, spawner: Spawner, *, default_timeout: float = DEFAULT_TIMEOUT
  ):
    self._dispatcher = Dispatcher(default_timeout=default_timeout)
    self._dispatcher.bind(Runtime(transport, spawner, self._dispatcher))

  def on(self, kind: str, handler: RequestHandler) -> None:
    self._dispatcher.on(kind, handler)

  def add_delivery_observer(self, observer: DeliveryObserver) -> None:
    self._dispatcher.add_delivery_observer(observer)

  def run(self, root: LaunchSpec) -> int:
    return asyncio.run(self._dispatcher.run(root))

  def stop(self) -> None:
    self._dispatcher.stop()


# --- built-in kind handlers -------------------------------------------------


def ping_handler(context: Dispatcher, peer: Peer, message: Message) -> None:
  """the reserved `ping` kind: answer ok with the request's own arguments echoed back."""
  context.reply(peer, {'outcome': 'ok', 'value': message.args})


def spawn_test_handler(launch: LaunchSpec) -> RequestHandler:
  """a spawn-test kind handler bound to an injected `LaunchSpec`: spawns it as the
  worker of the requesting exchange, whose progress and result then flow back to the
  requester."""

  def handler(context: Dispatcher, peer: Peer, _message: Message) -> None:
    context.spawn(launch, peer)

  return handler
