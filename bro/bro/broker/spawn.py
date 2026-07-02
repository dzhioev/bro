"""spawn port — the Spawner launches a peer's process and returns a `ChildHandle`
the Broker supervises.
"""

from abc import ABC, abstractmethod

from broker.transport import Provisioned


class LaunchSpec:
  """opaque launch description — concrete fields live with the concrete Spawner adapter."""


class ChildHandle(ABC):
  """the Broker's handle on one spawned peer's process."""

  @abstractmethod
  async def wait(self) -> int: ...  # await exit → code

  @abstractmethod
  async def kill(self) -> None: ...

  # tail of the child's combined stdout+stderr for failure diagnosis ('' if the adapter
  # doesn't capture it). synchronous, unlike wait/kill: it reads an in-memory buffer,
  # with no I/O to await.
  @abstractmethod
  def output_tail(self) -> str: ...


class Spawner(ABC):
  """launch a peer's process and return a handle the Broker supervises."""

  @abstractmethod
  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle: ...
