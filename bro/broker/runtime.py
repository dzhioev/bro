"""Runtime — the mechanism layer: owns the asyncio loop and all mutable per-peer state.

The Runtime supervises peers (a peer is its channel) over the async `ServerTransport`
and `Spawner`, and reports raw, symmetric lifecycle events to a synchronous, Dispatcher-
shaped `Listener` — never synthesizing protocol messages. It owns no topology, no
correlation, no notion of a bro or a session. All of it runs on one event loop, so
every mutation happens in a loop callback and there is no lock.

Two invariants carry the design:

- **Death = process exit, not socket EOF.** A peer's connection is transient (an fd can
  outlive the process, and a dropped socket can reconnect while the process lives), so
  EOF is a *channel* fact. The reliable death signal is `await handle.wait()`; a wait
  task per peer emits `on_exit(peer, code, output)` when the process is reaped.
- **Drain-before-decide.** On process exit, before emitting `on_exit`, the Runtime waits
  (bounded) for the transport to flush the channel to EOF — reusing `on_disconnect` as
  the "channel drained" marker. A result the child wrote just before exiting is
  already in the host buffer, so the flush delivers it as an `on_message` first and the
  Dispatcher closes the exchange on it; `on_exit` then has nothing to synthesize.
  No exit⋀EOF join.

Birth is `on_connect` (socket accepted) — a peer is alive from when it attaches, not
from its first message (a `--raw` root may never send one).The request-lifecycle
timeout is a `call_later` timer: on fire, the Runtime kills the peer and emits
`on_timeout` (already killed); the subsequent `on_exit` is the Dispatcher's to dedupe.
The root is a uniform peer — its only residual specialness (its exit ends the session)
lives in the facade, not here.

An *expected* peer (`expect()`) is the external variant of a spawned one: the channel
is provisioned, but the process that will attach to it is launched by someone else, so
there is no `ChildHandle` and the death invariant inverts — with no process to reap,
channel EOF after an attach *is* the death signal (`on_gone`), reliable because the
consumers that attach hold one connection for their whole run and never reconnect.
`kill` for an expected peer closes the channel host-side; the external process is not
ours to kill and simply loses its channel.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional, Protocol

from bro.base import log
from bro.broker.brotocol import Message
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import ChannelID, Provisioned, ServerTransport

Peer = ChannelID  # a peer is its channel

# how long to wait for the channel to flush to EOF after the process exits, before
# emitting on_exit. EOF follows process exit within milliseconds; the bound only guards
# the pathological case of an fd inherited by a process that outlives this peer.
_DRAIN_TIMEOUT = 2.0


class Listener(Protocol):
  """the Runtime reports lifecycle up to this; the Dispatcher implements it, on the loop."""

  def on_connect(self, peer: Peer) -> None: ...
  def on_message(self, peer: Peer, message: Message) -> None: ...
  def on_exit(self, peer: Peer, code: int, output: str) -> None: ...
  def on_timeout(self, peer: Peer) -> None: ...
  def on_gone(self, peer: Peer) -> None: ...


@dataclass
class _PeerState:
  channel: ChannelID
  disconnected: asyncio.Event = field(default_factory=asyncio.Event)  # set on channel EOF
  connected: bool = False  # on_connect fired at least once (gates birth + the drain wait)
  external: bool = False  # an expected peer: no handle, death = channel EOF (see docstring)
  handle: Optional[ChildHandle] = None
  wait_task: Optional[asyncio.Task] = None
  timer: Optional[asyncio.TimerHandle] = None


class Runtime:
  def __init__(self, transport: ServerTransport, spawner: Spawner, listener: Listener):
    self._transport = transport
    self._spawner = spawner
    self._listener = listener
    self._peers: dict[ChannelID, _PeerState] = {}

  # --- commands (called on the loop) --------------------------------------

  async def spawn(self, launch: LaunchSpec, *, timeout: Optional[float], exchange: str) -> Peer:
    """provision a channel, launch the peer, and supervise it; `exchange` passes
    through to the spawner (the worker-launch contract: a peer gets its channel
    and the id of the exchange it answers). A launch failure rolls back its own
    registration and re-raises."""
    provisioned = await self._transport.provision()
    channel = provisioned.channel
    peer = _PeerState(channel=channel)
    self._peers[channel] = peer
    try:
      peer.handle = await self._spawner.spawn(launch, provisioned, exchange)
    except BaseException:
      del self._peers[channel]
      await self._transport.close(channel)
      raise
    peer.wait_task = asyncio.create_task(self._await_exit(channel))
    if timeout is not None:
      loop = asyncio.get_running_loop()
      peer.timer = loop.call_later(timeout, self._fire_timeout, channel)
    return channel

  async def expect(self, *, timeout: Optional[float]) -> Provisioned:
    """provision a channel for an externally launched peer and supervise it —
    death is channel EOF after an attach (`on_gone`), not process exit. Returns
    the provisioned channel so the caller can hand its endpoint to whatever
    launches the peer."""
    provisioned = await self._transport.provision()
    channel = provisioned.channel
    peer = _PeerState(channel=channel, external=True)
    self._peers[channel] = peer
    peer.wait_task = asyncio.create_task(self._await_gone(channel))
    if timeout is not None:
      loop = asyncio.get_running_loop()
      peer.timer = loop.call_later(timeout, self._fire_timeout, channel)
    return provisioned

  def send(self, peer: Peer, message: Message) -> None:
    self._schedule(self._transport.send(peer, message))

  def kill(self, peer: Peer) -> None:
    state = self._peers.get(peer)
    if state is None:
      return
    if state.handle is not None:
      self._schedule(state.handle.kill())
      return
    if state.external:
      # not our process to kill: closing the channel is the whole kill. The
      # host-side close fires no on_disconnect (the adapter reserves that for
      # peer drops), so mark the channel gone here for the wait task.
      self._schedule(self._transport.close(peer))
      state.disconnected.set()

  def forget(self, peer: Peer) -> None:
    """drop the channel + timer + reap state. The process (if still alive) is left to
    the Dispatcher's kill; forget only reclaims the Runtime's bookkeeping."""
    state = self._peers.pop(peer, None)
    if state is None:
      return
    self._cancel_timer(state)
    wait_task = state.wait_task
    if wait_task is not None and wait_task is not asyncio.current_task():
      wait_task.cancel()
    self._schedule(self._transport.close(peer))

  async def serve(self) -> None:
    await self._transport.serve(self)

  async def stop(self) -> None:
    """hard teardown: kill every remaining peer, cancel its timer + wait task, and shut
    the transport down. No on_exit fires for a stopped peer (its wait task is cancelled)."""
    states = list(self._peers.values())
    self._peers.clear()
    for state in states:
      self._cancel_timer(state)
      if state.wait_task is not None:
        state.wait_task.cancel()
    await asyncio.gather(
      *(s.handle.kill() for s in states if s.handle is not None), return_exceptions=True
    )
    await asyncio.gather(
      *(s.wait_task for s in states if s.wait_task is not None), return_exceptions=True
    )
    await self._transport.shutdown()

  # --- Sink (the transport calls these on the loop) -----------------------

  async def on_connect(self, channel: ChannelID) -> None:
    state = self._peers.get(channel)
    if state is None:
      return
    state.disconnected.clear()  # reset for a (re)connection
    if not state.connected:
      state.connected = True
      self._listener.on_connect(channel)

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    if channel in self._peers:
      self._listener.on_message(channel, message)

  async def on_disconnect(self, channel: ChannelID) -> None:
    state = self._peers.get(channel)
    if state is not None:
      state.disconnected.set()  # channel EOF — a drain marker, not death

  # --- supervision --------------------------------------------------------

  async def _await_exit(self, channel: ChannelID) -> None:
    state = self._peers.get(channel)
    if state is None or state.handle is None:
      return
    code = await state.handle.wait()
    if self._peers.get(channel) is not state:  # forgotten while waiting
      return
    await self._drain(state)
    if self._peers.get(channel) is not state:
      return
    self._cancel_timer(state)
    self._listener.on_exit(channel, code, state.handle.output_tail())

  async def _await_gone(self, channel: ChannelID) -> None:
    """the expected-peer counterpart of `_await_exit`: with no process to reap,
    the channel going away is the death signal. `disconnected` is set by a peer
    drop after an attach, or by `kill` closing the channel host-side; either way
    every frame the peer wrote was already delivered in order before EOF, so no
    drain is needed."""
    state = self._peers.get(channel)
    if state is None:
      return
    await state.disconnected.wait()
    if self._peers.get(channel) is not state:  # forgotten while waiting
      return
    self._cancel_timer(state)
    self._listener.on_gone(channel)

  async def _drain(self, state: _PeerState) -> None:
    """flush the channel before deciding: wait (bounded) for the transport to read the
    connection to EOF, so any frame the peer wrote just before exiting is delivered as
    on_message first. Skipped when the peer never attached (nothing was buffered)."""
    if not state.connected:
      return
    try:
      await asyncio.wait_for(state.disconnected.wait(), _DRAIN_TIMEOUT)
    except TimeoutError:
      pass  # fd outlived the process; proceed without a full drain

  def _fire_timeout(self, channel: ChannelID) -> None:
    state = self._peers.get(channel)
    if state is None:
      return
    state.timer = None
    self.kill(channel)
    self._listener.on_timeout(channel)

  def _cancel_timer(self, state: _PeerState) -> None:
    if state.timer is not None:
      state.timer.cancel()
      state.timer = None

  def _schedule(self, coroutine) -> None:
    task = asyncio.ensure_future(coroutine)
    task.add_done_callback(self._report_task)

  def _report_task(self, task: asyncio.Task) -> None:
    if task.cancelled():
      return
    exception = task.exception()
    if exception is not None:
      log.warning(f'broker runtime: background task failed: {exception!r}')
