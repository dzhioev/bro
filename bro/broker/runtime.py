"""Shape-free broker runtime over one transport."""

import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol

from bro.base import log
from bro.broker.brotocol import Message
from bro.broker.transport import ChannelID, Provisioned, ServerTransport

Peer = ChannelID


class ChannelEvents(Protocol):
  def on_connect(self) -> None: ...
  def on_message(self, message: Message) -> None: ...
  def on_disconnect(self) -> None: ...


class Runtime:
  """Own transport serving, channel demultiplexing, delivery, and teardown."""

  def __init__(self, transport: ServerTransport):
    self._transport = transport
    self._channels: dict[ChannelID, ChannelEvents] = {}

  async def provision(self, events: ChannelEvents) -> Provisioned:
    provisioned = await self._transport.provision()
    self._channels[provisioned.channel] = events
    return provisioned

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
