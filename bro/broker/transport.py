"""transport ports: the host↔peer channel abstraction the Broker routes over.

Defines the substrate-facing contracts — `ServerTransport` (host side, async: its
methods and the `Sink` callbacks run on the broker's event loop) and
`ClientTransport` (peer side, synchronous: a peer is a separate process) — plus the
`Sink` the Broker implements to receive inbound traffic, the `Provisioned` channel
handle, the `Address`/`ChannelID` aliases, and `connect(address)` which maps a URI
scheme to its client adapter.

A transport guarantees exactly one thing beyond byte delivery: *channel
authenticity*. Every message handed to `Sink.on_message` is attributed to the
channel the connection attached as; there is no forgeable `from` field. The tcp
adapter pins that at connect, on the channel token the peer was handed.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from bro.broker.brotocol import Message

Address = str  # channel URI carried in BROKER_CHANNEL, e.g. 'tcp://<token>@127.0.0.1:7321'
ChannelID = str  # opaque host-side handle for one peer's channel


@dataclass(frozen=True)
class Provisioned:
  channel: ChannelID
  host_endpoint: Any  # opaque to the broker; the Spawner renders it (tcp: port + token)


class Sink(Protocol):
  """the Broker implements this; the ServerTransport calls it on the event loop.

  Symmetric channel lifecycle: on_connect at accept, on_disconnect on a
  peer-initiated drop (a host-side close/shutdown suppresses it). on_connect
  precedes any on_message for that channel.
  """

  async def on_connect(self, channel: ChannelID) -> None: ...
  async def on_message(self, channel: ChannelID, message: Message) -> None: ...
  async def on_disconnect(self, channel: ChannelID) -> None: ...


class ServerTransport(ABC):
  """host side — provisions channels and serves them on the broker's event loop."""

  @abstractmethod
  async def provision(self) -> Provisioned: ...

  @abstractmethod
  async def serve(self, sink: Sink) -> None: ...

  @abstractmethod
  async def send(self, channel: ChannelID, message: Message) -> None: ...

  @abstractmethod
  async def close(self, channel: ChannelID) -> None: ...

  @abstractmethod
  async def shutdown(self) -> None: ...


class ClientTransport(ABC):
  """peer side — one channel back to the host (synchronous; a peer is its own process)."""

  @abstractmethod
  def send(self, message: Message) -> None: ...

  @abstractmethod
  def receive(self, timeout: Optional[float]) -> Optional[Message]: ...

  @abstractmethod
  def close(self, confirm: bool = False) -> None:
    """close the channel. With `confirm`, first block for the receiver to confirm
    — by closing back — that everything sent was consumed. Both receivers consume
    frames strictly in order before they see the sender's EOF (the host adapter's
    read loop; the broxy forwards each frame upstream, drained, before the next
    read), so the close-back is that guarantee. A peer whose last send precedes
    its own exit needs it: without the handshake, frames still buffered in an
    intermediary (the broxy, killed with the container's pid namespace) can die
    with the sender. The wait is deliberately unbounded — a deadline that lets
    close() return unconfirmed would reintroduce the race it exists to remove;
    bounding a wedged receiver is the supervisor's job (the host kills a
    timed-out child), not the sender's. A receiver already gone closes the
    socket, ending the wait immediately.

    A plain close (no `confirm`) also aborts a concurrent `receive` blocked on
    the same transport from another thread — the blocked call returns as if the
    channel reached EOF. This is how a controller cancels an off-thread wait it
    abandoned (the summon service tools ride on it); an adapter must implement
    close so that wake-up is reliable, not incidental (a bare fd close is not —
    the tcp adapter wakes the reader through a self-pipe)."""


def connect(address: Address) -> ClientTransport:
  scheme, separator, _ = address.partition(':')
  if separator == '':
    raise ValueError(f'broker address missing scheme: {address!r}')
  from bro.broker.transports import tcp

  if scheme == tcp.SCHEME:
    return tcp.TcpClientTransport(address)
  raise ValueError(f'unsupported broker transport scheme: {scheme!r}')
