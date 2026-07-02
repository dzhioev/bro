"""transport ports: the host↔peer channel abstraction the Broker routes over.

Defines the substrate-facing contracts — `ServerTransport` (host side, async: its
methods and the `Sink` callbacks run on the broker's event loop) and
`ClientTransport` (peer side, synchronous: a peer is a separate process) — plus the
`Sink` the Broker implements to receive inbound traffic, the `Provisioned` channel
handle, the `Address`/`ChannelID` aliases, and `connect(address)` which maps a URI
scheme to its client adapter.

A transport guarantees exactly one thing beyond byte delivery: *channel
authenticity*. Every message handed to `Sink.on_message` is attributed to the
channel it physically arrived on; there is no forgeable `from` field. Each adapter
enforces this its own way — the unix adapter dedicates one socket per peer (a peer
can reach only the socket it was handed); a websocket adapter would pin by
token/cert at connect.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from broker.brotocol import Message

Address = str  # connection URI carried in BROKER_CHANNEL, e.g. 'unix:/run/broker.sock'
ChannelID = str  # opaque host-side handle for one peer's channel


@dataclass(frozen=True)
class Provisioned:
  channel: ChannelID
  host_endpoint: Any  # opaque to the broker; the Spawner wires it (socket path / url+token)


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
  def close(self) -> None: ...


def connect(address: Address) -> ClientTransport:
  scheme, sep, rest = address.partition(':')
  if sep == '':
    raise ValueError(f'broker address missing scheme: {address!r}')
  if scheme == 'unix':
    from broker.transports.unix import UnixClientTransport

    return UnixClientTransport(rest)
  raise ValueError(f'unsupported broker transport scheme: {scheme!r}')
