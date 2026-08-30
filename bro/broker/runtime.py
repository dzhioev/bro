"""Shape-free broker runtime over transport and process-launch ports."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Protocol

from bro.base import log
from bro.broker.brotocol import Message
from bro.broker.job import CommandJob, launch as launch_job
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import ChannelID, Provisioned, ServerTransport

Peer = ChannelID


class ChannelEvents(Protocol):
  def on_connect(self) -> None: ...
  def on_message(self, message: Message) -> None: ...
  def on_disconnect(self) -> None: ...


class Runtime:
  """Own the event loop's transport and process-launch mechanisms.

  Per-shape supervision belongs to ``Worker`` implementations. The runtime only
  provisions and closes channels, launches and reaps processes, and demultiplexes
  channel lifecycle to the worker that owns each channel.
  """

  def __init__(self, transport: ServerTransport, spawner: Spawner):
    self._transport = transport
    self._spawner = spawner
    self._channels: dict[ChannelID, ChannelEvents] = {}

  async def provision(self, events: ChannelEvents) -> Provisioned:
    provisioned = await self._transport.provision()
    self._channels[provisioned.channel] = events
    return provisioned

  async def launch(self, launch: LaunchSpec, provisioned: Provisioned, quest: str) -> ChildHandle:
    return await self._spawner.spawn(launch, provisioned, quest)

  async def launch_job(self, command: CommandJob, directory: Path) -> ChildHandle:
    return await launch_job(command, directory)

  def send(self, peer: Peer, message: Message) -> None:
    self._schedule(self._transport.send(peer, message))

  async def close(self, peer: Peer) -> None:
    self._channels.pop(peer, None)
    await self._transport.close(peer)

  async def serve(self) -> None:
    await self._transport.serve(self)

  async def stop(self) -> None:
    self._channels.clear()
    await self._transport.shutdown()

  async def on_connect(self, channel: ChannelID) -> None:
    events = self._channels.get(channel)
    if events is not None:
      events.on_connect()

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    events = self._channels.get(channel)
    if events is not None:
      events.on_message(message)

  async def on_disconnect(self, channel: ChannelID) -> None:
    events = self._channels.get(channel)
    if events is not None:
      events.on_disconnect()

  def _schedule(self, coroutine: Coroutine[Any, Any, Any]) -> None:
    task = asyncio.ensure_future(coroutine)
    task.add_done_callback(self._report_task)

  @staticmethod
  def _report_task(task: asyncio.Task) -> None:
    if task.cancelled():
      return
    exception = task.exception()
    if exception is not None:
      log.warning('broker runtime: background task failed: %r', exception)
