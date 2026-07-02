"""Dispatcher — the logic layer: topology, correlation, the routing rules, handlers.

The `Runtime` (mechanism) reports raw, symmetric lifecycle up to this `Dispatcher`
(logic), which owns everything protocol: the peer graph (`parent`/`children`), the
correlation state (`origin[peer]`, `pending[in_reply_to]`), the `finalized` set, the
handler registry, the four routing rules, and the synthesis of `failed`. It runs only
inside `Runtime` callbacks, all on the one event loop, so it is a plain synchronous
object with no lock.

Two invariants carry the design:

- **`finalized` ⇒ exactly one terminal.** A peer is finalized when its `completed` is
  routed or a `failed` is synthesized for it. The set gates every later message from
  that peer (drop-after-terminal) and suppresses redundant `failed` synthesis, so
  whichever of `completed` / exit / timeout is processed first wins and the rest are
  dropped — closing the completed-vs-exit and timeout-vs-completed double-terminal races.
- **`failed` is the only synthesized event.** Children emit `started` / `completed`; the
  Dispatcher never fabricates those. `failed` is reserved for process-level death a child
  never got to report — an `on_exit` without a preceding `completed` (`reason='exit'`) or
  an `on_timeout` (`reason='timeout'`, after the Runtime already killed the peer).

The root is a uniform peer: `run(root)` spawns it like any other and awaits its `on_exit`.
Its only residual specialness is that its exit ends the session (it has no origin, so no
`failed` is synthesized for it — there is no parent to notify).
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Optional, Protocol

from base import log
from broker.brotocol import Message, Tag
from broker.runtime import Peer, Runtime
from broker.spawn import LaunchSpec, Spawner
from broker.transport import ServerTransport

# request-lifecycle bound for a spawned child (LLM children run for minutes)
DEFAULT_TIMEOUT = 600.0

# a handler receives the Dispatcher as its `context` and drives the routing primitives.
RequestHandler = Callable[['Dispatcher', Peer, Message], None]

# messages a spawned child emits over its own lifecycle; rule 2 routes these to its parent.
_LIFECYCLE_TAGS = frozenset({Tag.STARTED, Tag.COMPLETED})


class RuntimeCommands(Protocol):
  """the mechanism-layer commands the Dispatcher issues; the real `Runtime` satisfies it."""

  async def spawn(self, launch: LaunchSpec, *, timeout: Optional[float]) -> Peer: ...
  def send(self, peer: Peer, message: Message) -> None: ...
  def kill(self, peer: Peer) -> None: ...
  def forget(self, peer: Peer) -> None: ...
  async def serve(self) -> None: ...
  async def stop(self) -> None: ...


class Dispatcher:
  def __init__(self, *, default_timeout: float = DEFAULT_TIMEOUT):
    self._runtime: Optional[RuntimeCommands] = None
    self._default_timeout = default_timeout
    self.parent: dict[Peer, Peer] = {}
    self.children: dict[Peer, set[Peer]] = {}
    self.origin: dict[Peer, tuple[Peer, str]] = {}  # spawned peer -> (parent, spawning request id)
    self.pending: dict[str, Peer] = {}  # request id -> the peer awaiting its reply
    self.finalized: set[Peer] = set()
    self._handlers: dict[str, RequestHandler] = {}
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

  def on(self, message_type: str, handler: RequestHandler) -> None:
    self._handlers[message_type] = handler

  # --- routing primitives (used by the rules; a handler drives the same set) ----

  def deliver(self, peer: Peer, message: Message) -> None:
    self.runtime.send(peer, message)

  def reply(self, peer: Peer, payload: dict) -> None:
    """send a generic correlated reply (`Tag.REPLY`) to the in-flight request's origin peer."""
    self.deliver(peer, Message(type=Tag.REPLY, payload=payload, in_reply_to=self._request_id()))

  def refuse(self, peer: Peer, message: Message) -> None:
    """decline an unroutable message (rule 4). v1 is parent/child-only, so anything outside
    that topology is dropped with a log line rather than delivered."""
    log.warning(f'broker dispatcher: refused {message.type!r} from peer {peer} (no route)')

  def spawn(self, launch: LaunchSpec, parent: Peer, *, timeout: Optional[float] = None) -> None:
    """spawn `launch` as a child of `parent`, correlated to the in-flight request. Schedules
    the async `Runtime.spawn` and registers topology when it resolves — the child cannot send
    a frame until it has been launched and has connected, both strictly after registration."""
    in_reply_to = self._request_id()
    effective_timeout = timeout if timeout is not None else self._default_timeout
    task = asyncio.ensure_future(self.runtime.spawn(launch, timeout=effective_timeout))

    def _registered(finished: asyncio.Task) -> None:
      if finished.cancelled():
        return
      error = finished.exception()
      if error is not None:
        log.warning(f'broker dispatcher: spawn failed: {error!r}')
        return
      self._register_child(finished.result(), parent, in_reply_to)

    task.add_done_callback(_registered)

  def invoke(self, peer: Peer, message: Message) -> None:
    """dispatch a fresh typed request to its registered handler (rule 3), exposing the request
    to `reply` / `spawn` as the in-flight one for the duration of the call."""
    handler = self._handlers[message.type]
    previous = self._active
    self._active = message
    try:
      handler(self, peer, message)
    finally:
      self._active = previous

  # --- Runtime listener (all on the loop) ---------------------------------

  def on_connect(self, peer: Peer) -> None:
    del peer  # topology is registered at spawn time, so birth needs no dispatcher action

  def on_message(self, peer: Peer, message: Message) -> None:
    if peer in self.finalized:
      return  # drop-after-terminal
    if self._route(peer, message) and message.type == Tag.COMPLETED:
      self.finalized.add(peer)

  def on_exit(self, peer: Peer, code: int, output: str) -> None:
    if peer not in self.finalized:
      self._fail(peer, {'reason': 'exit', 'exit_code': code, 'output_tail': output})
    self._cleanup(peer)
    if peer == self._root and self._root_exit is not None and not self._root_exit.done():
      self._root_exit.set_result(code)

  def on_timeout(self, peer: Peer) -> None:
    # the Runtime already killed the peer; the following on_exit dedupes on `finalized`.
    if peer not in self.finalized:
      self._fail(peer, {'reason': 'timeout'})
      self.finalized.add(peer)

  # --- the four routing rules, checked in order ---------------------------

  def _route(self, peer: Peer, message: Message) -> bool:
    """apply the routing rules; return True when the source's own message was delivered
    onward (rules 1-2), False when it was invoked or refused (rules 3-4)."""
    awaiter = self.pending.get(message.in_reply_to) if message.in_reply_to is not None else None
    if awaiter is not None:  # rule 1: a reply to an awaited request -> its requester, as-is
      self.deliver(awaiter, message)
      return True
    origin = self.origin.get(peer)
    if origin is not None and message.type in _LIFECYCLE_TAGS:  # rule 2: child lifecycle -> parent
      parent, in_reply_to = origin
      self.deliver(parent, replace(message, in_reply_to=in_reply_to))
      return True
    if message.in_reply_to is None and message.type in self._handlers:  # rule 3: fresh request
      self.invoke(peer, message)
      return False
    self.refuse(peer, message)  # rule 4
    return False

  # --- facade support -----------------------------------------------------

  async def run(self, root: LaunchSpec) -> int:
    """serve, spawn the root as a uniform peer (no request-lifecycle timeout), await its exit,
    then tear down any outstanding children and return the root's exit code."""
    self._root_exit = asyncio.get_running_loop().create_future()
    serve_task = asyncio.ensure_future(self.runtime.serve())
    await asyncio.sleep(0)  # let serve install the sink before the root connects
    self._root = await self.runtime.spawn(root, timeout=None)
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
    return self._active.id

  def _register_child(self, peer: Peer, parent: Peer, in_reply_to: str) -> None:
    self.parent[peer] = parent
    self.children.setdefault(parent, set()).add(peer)
    self.origin[peer] = (parent, in_reply_to)
    self.pending[in_reply_to] = parent

  def _fail(self, peer: Peer, payload: dict) -> None:
    """synthesize a `failed` to the peer's origin parent; a peer with no origin (the root) has
    nobody to notify, so its death is silent to the graph."""
    origin = self.origin.get(peer)
    if origin is None:
      return
    parent, in_reply_to = origin
    self.deliver(parent, Message(type=Tag.FAILED, payload=payload, in_reply_to=in_reply_to))

  def _cleanup(self, peer: Peer) -> None:
    """drop the peer from the graph, correlation state, and the Runtime's bookkeeping."""
    origin = self.origin.pop(peer, None)
    if origin is not None:
      _, in_reply_to = origin
      self.pending.pop(in_reply_to, None)
    parent = self.parent.pop(peer, None)
    if parent is not None:
      siblings = self.children.get(parent)
      if siblings is not None:
        siblings.discard(peer)
    self.children.pop(peer, None)
    self.finalized.discard(peer)
    self.runtime.forget(peer)


class Broker:
  """the thin facade cw constructs: injects the two ports, exposes `on` / `run` / `stop`."""

  def __init__(
    self, transport: ServerTransport, spawner: Spawner, *, default_timeout: float = DEFAULT_TIMEOUT
  ):
    self._dispatcher = Dispatcher(default_timeout=default_timeout)
    self._dispatcher.bind(Runtime(transport, spawner, self._dispatcher))

  def on(self, message_type: str, handler: RequestHandler) -> None:
    self._dispatcher.on(message_type, handler)

  def run(self, root: LaunchSpec) -> int:
    return asyncio.run(self._dispatcher.run(root))

  def stop(self) -> None:
    self._dispatcher.stop()


# --- built-in acceptance handlers -------------------------------------------


def ping_handler(context: Dispatcher, peer: Peer, message: Message) -> None:
  """reply to a `ping` request with its payload echoed back, correlated to the request."""
  context.reply(peer, {'pong': message.payload})


def spawn_test_handler(launch: LaunchSpec) -> RequestHandler:
  """a `spawn`-test handler bound to an injected `LaunchSpec`: spawns it as a child of the
  requester, whose `started` / `completed` then route back as the reply to the spawn request."""

  def handler(context: Dispatcher, peer: Peer, _message: Message) -> None:
    context.spawn(launch, peer)

  return handler
