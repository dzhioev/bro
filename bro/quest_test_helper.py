"""the live broker and stub-server harnesses the summon and quest CLI tests drive."""

import asyncio
import contextlib
import queue
import threading
from dataclasses import dataclass

from bro.broker import brotocol
from bro.broker.brotocol import Message, Talk
from bro.broker.dispatcher import Broker, Dispatcher
from bro.broker.environment import BROKER_CHANNEL, BROKER_MISSION
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import ChannelID, Provisioned
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.quest import SUMMON
from bro.summon import RUNTIME_ENV

TIMEOUT = 5.0


class StubSink:
  def __init__(self):
    self.messages: asyncio.Queue = asyncio.Queue()

  async def on_connect(self, channel: ChannelID) -> None:
    pass

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    self.messages.put_nowait((channel, message))

  async def on_disconnect(self, channel: ChannelID) -> None:
    pass


@dataclass
class Harness:
  transport: TcpServerTransport
  sink: StubSink


class LiveHandle(ChildHandle):
  def __init__(self):
    self._done = threading.Event()
    self._lock = threading.Lock()
    self._exit_code: int | None = None

  def finish(self, exit_code: int) -> None:
    with self._lock:
      if self._exit_code is None:
        self._exit_code = exit_code
        self._done.set()

  async def wait(self) -> int:
    await asyncio.to_thread(self._done.wait)
    assert self._exit_code is not None
    return self._exit_code

  async def kill(self) -> None:
    self.finish(-15)

  def output_tail(self) -> str:
    return ''


@dataclass(frozen=True)
class SpawnedEndpoint:
  channel: Provisioned
  quest: str
  talk: Talk
  handle: LiveHandle


class LiveSpawner(Spawner):
  def __init__(self):
    self.spawned: queue.Queue[SpawnedEndpoint] = queue.Queue()

  async def spawn(
    self, launch: LaunchSpec, channel: Provisioned, mission: str, talk: Talk
  ) -> ChildHandle:
    del launch
    handle = LiveHandle()
    self.spawned.put(SpawnedEndpoint(channel, mission, talk, handle))
    return handle


@contextlib.asynccontextmanager
async def running_live_broker():
  spawner = LiveSpawner()
  broker = Broker(TcpServerTransport([LOCAL_HOST]))

  def spawn_summon(context: Dispatcher, peer, message: Message) -> None:
    talk: Talk = frozenset(message.args.get('talk', []))
    context.spawn(LaunchSpec(), spawner, peer, type='bro', talk=talk, timeout=None)

  broker.on(SUMMON, spawn_summon)
  broker_task = asyncio.create_task(
    asyncio.to_thread(broker.run, LaunchSpec(), spawner, type='bro')
  )
  root = await asyncio.to_thread(spawner.spawned.get, True, TIMEOUT)
  try:
    yield spawner, root
  finally:
    root.handle.finish(0)
    await asyncio.wait_for(broker_task, TIMEOUT)


@contextlib.asynccontextmanager
async def running_server(monkeypatch):
  transport = TcpServerTransport([LOCAL_HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)
  provisioned = await transport.provision()
  monkeypatch.setenv(BROKER_CHANNEL, provisioned.host_endpoint.address(LOCAL_HOST))
  monkeypatch.setenv(BROKER_MISSION, 'ROOT')
  monkeypatch.setenv(RUNTIME_ENV, '/runtime')
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def next_message(server: Harness) -> tuple[ChannelID, Message]:
  return await asyncio.wait_for(server.sink.messages.get(), TIMEOUT)


def message_id(message: Message) -> str:
  assert message.id is not None
  return message.id


async def reply(server: Harness, channel: ChannelID, request: Message, **payload) -> None:
  await server.transport.send(channel, brotocol.result(message_id(request), **payload))


def quest_record(
  quest_id: str,
  state: str,
  *,
  result: dict | None = None,
  kind: str = 'summon',
  trail_id: str | None = None,
  **overrides,
) -> dict:
  """a by-id journal view; a listing view is the same with `pending` in place of `messages`."""
  quest = {
    'id': quest_id,
    'kind': kind,
    'parent': 'ROOT',
    'args': {'target': 'dev', 'prompt': 'work'},
    'state': state,
    'talk': [],
    'messages': [],
    'chat_seq': 0,
  }
  if result is not None:
    quest['result'] = result
  if trail_id is not None:
    quest['trail_id'] = trail_id
  quest.update(overrides)
  return quest


def entry(
  seq: int,
  sender: str,
  text: str,
  *,
  id: str | None = None,
  reply_to: str | None = None,
  pending: bool = False,
  transition: str = 'message',
  reason: str | None = None,
) -> dict:
  """one retained conversation entry as the by-id view carries it."""
  record: dict = {
    'seq': seq,
    'at': 'now',
    'transition': transition,
    'from': sender,
    'head': {'text': text},
  }
  if id is not None:
    record['id'] = id
  if reply_to is not None:
    record['reply_to'] = reply_to
  if reason is not None:
    record['reason'] = reason
  if pending:
    record['pending'] = True
  return record
