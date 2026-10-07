import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

import pytest

from bro import mission, quest, summon, turn_end, watches
from bro.broker import brotocol
from bro.broker.client import Client
from bro.broker.environment import BROKER_CHANNEL, BROKER_MISSION, BROKER_TALK
from bro.broker.transport import connect
from bro.broker.transports.tcp import LOCAL_HOST
from bro.quest_test_helper import TIMEOUT, SpawnedEndpoint, running_live_broker


@dataclass
class FakeWatch:
  command: str
  alive: bool = True
  last: str | None = None
  head: int | None = 0

  def producer_alive(self) -> bool:
    return self.alive

  def last_complete_line(self) -> str | None:
    return self.last

  def journal_head(self) -> int | None:
    return self.head


@dataclass
class FakeStore:
  watches: list[FakeWatch] = field(default_factory=list)
  pending: bool = False
  notified: set[frozenset[str]] = field(default_factory=set)
  waited_for: list[int] = field(default_factory=list)

  def declared(self) -> list[FakeWatch]:
    return self.watches

  def has_pending_lines(self) -> bool:
    return self.pending

  def mark_notified(self, live_set: frozenset[str]) -> bool:
    if live_set in self.notified:
      return False
    self.notified.add(live_set)
    return True

  def wait_for_journal_head(self, command: str, target: int) -> bool:
    self.waited_for.append(target)
    watch = next(watch for watch in self.watches if watch.command == command)
    watch.head = target
    return True


@dataclass(eq=True)
class FakePort:
  def __init__(self, store: FakeStore, background: tuple[str, ...] = ()) -> None:
    self.watch_store = cast(watches.Store, store)
    self.background = background
    self.notifications: list[str] = []
    self.ends = 0

  def background_work(self) -> tuple[str, ...]:
    return self.background

  def notify(self, text: str) -> None:
    self.notifications.append(text)

  def end(self) -> None:
    self.ends += 1


@pytest.fixture(autouse=True)
def live_facts(monkeypatch):
  monkeypatch.setattr(watches, 'session_watch_admitted', lambda: False)
  monkeypatch.setattr(turn_end.summon, 'summoned', lambda: False)
  monkeypatch.setattr(turn_end.summon, 'talk', lambda: ())
  monkeypatch.setattr(turn_end.mission, 'event_head', lambda: 0)


def _mission(mission_id: str, worker_type: str = 'bro') -> mission.LiveMission:
  label = 'dev' if worker_type == 'bro' else worker_type
  return mission.LiveMission(mission_id, worker_type, label)


def _with_broker(monkeypatch, *missions: mission.LiveMission, reply_awaited: bool = False) -> None:
  monkeypatch.setenv(BROKER_CHANNEL, 'unused')
  monkeypatch.setattr(turn_end.mission, 'live_missions', lambda: list(missions))
  monkeypatch.setattr(turn_end, '_reply_awaited', lambda: reply_awaited)


@pytest.mark.parametrize(
  'store,missions,reply_awaited',
  [
    (FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)]), (_mission('Q1'),), False),
    (FakeStore([FakeWatch('tail -f log')]), (), False),
    (FakeStore(pending=True), (), False),
    (FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)]), (), True),
  ],
)
def test_silent_wait_verdict_precedes_background_work(monkeypatch, store, missions, reply_awaited):
  _with_broker(monkeypatch, *missions, reply_awaited=reply_awaited)
  port = FakePort(store, ('task-1',))

  turn_end.settle(port)

  assert port.notifications == []
  assert port.ends == 0


def test_settlement_waits_for_the_session_watch_journal_head(monkeypatch):
  _with_broker(monkeypatch)
  monkeypatch.setattr(turn_end.mission, 'event_head', lambda: 7)
  watch = FakeWatch(watches.SESSION_WATCH_COMMAND, head=6)
  port = FakePort(FakeStore([watch]))

  turn_end.settle(port)

  assert port.ends == 1
  assert cast(FakeStore, port.watch_store).waited_for == [7]


def test_owner_led_background_work_notifies_then_keeps_waiting(monkeypatch):
  _with_broker(monkeypatch, _mission('WEB-1', 'webview'))
  monkeypatch.setattr(turn_end.summon, 'summoned', lambda: True)
  monkeypatch.setattr(turn_end.summon, 'talk', lambda: ('owner.say',))
  store = FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)])
  port = FakePort(store, ('browser task',))

  turn_end.settle(port)
  turn_end.settle(port)

  assert len(port.notifications) == 1
  notice = port.notifications[0]
  assert 'background work: browser task' in notice
  assert 'mission WEB-1: webview' in notice
  assert 'the summoner may still send a message' in notice
  assert 'Ending the turn again keeps waiting' in notice
  assert '`bro::answer`' in notice
  assert '`bro::quest_cancel`' not in notice
  assert port.ends == 0

  port.background = ('another task',)
  turn_end.settle(port)
  assert len(port.notifications) == 2


def test_uncovered_mission_notifies_once_then_ends(monkeypatch):
  _with_broker(monkeypatch, _mission('Q1'))
  port = FakePort(FakeStore())

  turn_end.settle(port)
  turn_end.settle(port)

  assert len(port.notifications) == 1
  assert 'quest Q1 to dev' in port.notifications[0]
  assert '`bro::quest_cancel`' in port.notifications[0]
  assert 'orphans the uncovered work' in port.notifications[0]
  assert port.ends == 1


def test_dead_session_watch_turns_session_traffic_into_notice_then_end(monkeypatch):
  monkeypatch.setattr(watches, 'session_watch_admitted', lambda: True)
  monkeypatch.setattr(turn_end.summon, 'summoned', lambda: True)
  monkeypatch.setattr(turn_end.summon, 'talk', lambda: ('owner.say',))
  _with_broker(monkeypatch, reply_awaited=True)
  store = FakeStore(
    [FakeWatch(watches.SESSION_WATCH_COMMAND, alive=False, last='[watch-run] exited 1')]
  )
  port = FakePort(store)

  turn_end.settle(port)
  turn_end.settle(port)

  assert len(port.notifications) == 1
  assert 'the summoner may still send a message' not in port.notifications[0]
  assert '[watch-run] exited 1' in port.notifications[0]
  assert port.ends == 1


def test_undelivered_exit_line_precedes_dead_watch_notice(monkeypatch):
  monkeypatch.setattr(watches, 'session_watch_admitted', lambda: True)
  store = FakeStore(
    [FakeWatch(watches.SESSION_WATCH_COMMAND, alive=False, last='[watch-run] exited 1')],
    pending=True,
  )
  port = FakePort(store)

  turn_end.settle(port)
  assert port.notifications == []
  assert port.ends == 0

  store.pending = False
  turn_end.settle(port)
  assert len(port.notifications) == 1


def test_no_live_work_ends_at_once():
  port = FakePort(FakeStore())

  turn_end.settle(port)

  assert port.notifications == []
  assert port.ends == 1


def _stand_as(monkeypatch, endpoint: SpawnedEndpoint) -> None:
  monkeypatch.setenv(BROKER_CHANNEL, endpoint.channel.host_endpoint.address(LOCAL_HOST))
  monkeypatch.setenv(BROKER_MISSION, endpoint.quest)
  monkeypatch.setenv(BROKER_TALK, brotocol.encode_talk(endpoint.talk))


@contextlib.asynccontextmanager
async def _summoned_session(
  monkeypatch, talk: tuple[str, ...]
) -> AsyncIterator[tuple[SpawnedEndpoint, SpawnedEndpoint]]:
  """a live broker holding a summoner and the session it summoned under `talk`;
  this process stands as the session."""
  monkeypatch.setattr(turn_end.summon, 'summoned', lambda: True)
  monkeypatch.setattr(turn_end.summon, 'talk', lambda: talk)
  async with running_live_broker() as (spawner, summoner):
    _stand_as(monkeypatch, summoner)
    with Client(connect(summoner.channel.host_endpoint.address(LOCAL_HOST))) as client:
      request = client.send(quest.LAUNCH, {'target': 'dev', 'prompt': 'work', 'talk': list(talk)})
      summon._await_acceptance(client, request)
      session = await asyncio.to_thread(spawner.spawned.get, True, TIMEOUT)
      _stand_as(monkeypatch, session)
      yield summoner, session


def _chat_past(quest_id: str, sequence: int) -> int:
  with mission.open_client() as client:
    record = mission.query_mission(client, quest_id, wait_seconds=TIMEOUT, since=sequence)
  assert record['chat_seq'] > sequence, record
  return record['chat_seq']


@pytest.mark.asyncio
async def test_the_sessions_own_question_waits_silently_until_the_summoner_replies(monkeypatch):
  port = FakePort(FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)]))
  async with _summoned_session(monkeypatch, ('worker.question',)) as (summoner, session):
    asked = await asyncio.to_thread(quest.ask, 'self', 'ship this?')
    asked_at = await asyncio.to_thread(_chat_past, session.quest, 0)

    await asyncio.to_thread(turn_end.settle, port)
    assert (port.notifications, port.ends) == ([], 0)

    _stand_as(monkeypatch, summoner)
    await asyncio.to_thread(quest.say, session.quest, 'yes', reply_to=asked.question_id)
    _stand_as(monkeypatch, session)
    await asyncio.to_thread(_chat_past, session.quest, asked_at)

    await asyncio.to_thread(turn_end.settle, port)
    assert (port.notifications, port.ends) == ([], 1)


@pytest.mark.asyncio
async def test_a_question_the_session_owes_its_summoner_is_no_reply_it_awaits(monkeypatch):
  port = FakePort(FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)]))
  async with _summoned_session(monkeypatch, ('owner.question',)) as (summoner, session):
    _stand_as(monkeypatch, summoner)
    await asyncio.to_thread(quest.ask, session.quest, 'which branch?')
    _stand_as(monkeypatch, session)
    await asyncio.to_thread(_chat_past, session.quest, 0)

    await asyncio.to_thread(turn_end.settle, port)

  [notice] = port.notifications
  assert 'the summoner may still send a message' in notice
  assert port.ends == 0


@pytest.mark.asyncio
async def test_a_reply_landing_after_the_journal_head_read_keeps_the_turn_waiting(monkeypatch):
  port = FakePort(FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)]))
  async with _summoned_session(monkeypatch, ('worker.question',)) as (summoner, session):
    asked = await asyncio.to_thread(quest.ask, 'self', 'ship this?')
    asked_at = await asyncio.to_thread(_chat_past, session.quest, 0)

    def head_read_before_the_reply() -> int:
      _stand_as(monkeypatch, summoner)
      quest.say(session.quest, 'yes', reply_to=asked.question_id)
      _stand_as(monkeypatch, session)
      _chat_past(session.quest, asked_at)
      return 0

    monkeypatch.setattr(turn_end.mission, 'event_head', head_read_before_the_reply)
    await asyncio.to_thread(turn_end.settle, port)

  assert (port.notifications, port.ends) == ([], 0)
