import asyncio
import contextlib
import time
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
from bro.run_lifecycle import RunLifecycle
from bro.watches import BeatSnapshot


@dataclass
class FakeWatch:
  command: str
  alive: bool = True
  last: str | None = None
  head: int | None = 0
  stalled: bool = False
  deadline: float | None = None
  snapshots: list[watches.BeatSnapshot | None] = field(default_factory=list)

  def producer_alive(self) -> bool:
    return self.alive

  def beat_snapshot(self) -> watches.BeatSnapshot | None:
    if len(self.snapshots) > 0:
      return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
    if self.stalled:
      return watches.BeatSnapshot('beat 1', 1.0)
    if self.deadline is not None:
      return watches.BeatSnapshot('beat 2', self.deadline)
    return None

  def last_complete_line(self) -> str | None:
    return self.last

  def journal_head(self) -> int | None:
    return self.head


@dataclass
class FakeStore:
  watches: list[FakeWatch] = field(default_factory=list)
  waking: bool = False
  notified: set[frozenset[str]] = field(default_factory=set)
  waited_for: list[int] = field(default_factory=list)
  reported: list[tuple[str, str]] = field(default_factory=list)

  def declared(self) -> list[FakeWatch]:
    return self.watches

  def has_waking_lines(self) -> bool:
    return self.waking

  def mark_stall_reported(self, command: str, snapshot: BeatSnapshot) -> None:
    self.reported.append((command, snapshot.text))

  def next_beat_deadline(self) -> float | None:
    deadlines = [watch.deadline for watch in self.watches if watch.deadline is not None]
    return min(deadlines) if len(deadlines) > 0 else None

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


def _work(identifier: str, last_active: float = 0.0) -> turn_end.BackgroundWork:
  return turn_end.BackgroundWork(identifier, f'{identifier} bg `sleep`', last_active)


class FakePort:
  def __init__(
    self, store: FakeStore, background: tuple[turn_end.BackgroundWork, ...] = ()
  ) -> None:
    self.watch_store = cast(watches.Store, store)
    self.silence_clock = turn_end.SilenceClock(patience=60.0, cap=240.0)
    self.background = background
    self.notifications: list[str] = []
    self.ends = 0

  def background_work(self) -> tuple[turn_end.BackgroundWork, ...]:
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


def _mission(mission_id: str, worker_type: str = 'bro') -> mission.LiveMission:
  label = 'dev' if worker_type == 'bro' else worker_type
  return mission.LiveMission(mission_id, worker_type, label)


def _with_broker(monkeypatch, *missions: mission.LiveMission, reply_awaited: bool = False) -> None:
  monkeypatch.setenv(BROKER_CHANNEL, 'unused')
  monkeypatch.setattr(turn_end.mission, 'live_missions', lambda: list(missions))
  monkeypatch.setattr(turn_end, '_reply_awaited', lambda: reply_awaited)
  monkeypatch.setattr(turn_end.mission, 'visible_event_head', lambda: 0)


@pytest.mark.parametrize(
  'store,missions,reply_awaited',
  [
    (FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)]), (_mission('Q1'),), False),
    (FakeStore([FakeWatch('tail -f log')]), (), False),
    (FakeStore(waking=True), (), False),
    (FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)]), (), True),
  ],
)
def test_a_live_wait_is_silent_beside_background_work(monkeypatch, store, missions, reply_awaited):
  _with_broker(monkeypatch, *missions, reply_awaited=reply_awaited)
  port = FakePort(store, (_work('task-1'),))

  turn_end.settle(port)

  assert port.notifications == []
  assert port.ends == 0


def test_settlement_waits_for_the_session_watch_journal_head(monkeypatch):
  _with_broker(monkeypatch)
  monkeypatch.setattr(turn_end.mission, 'visible_event_head', lambda: 7)
  watch = FakeWatch(watches.SESSION_WATCH_COMMAND, head=6)
  port = FakePort(FakeStore([watch]))

  turn_end.settle(port)

  assert port.ends == 1
  assert cast(FakeStore, port.watch_store).waited_for == [7]


def test_background_work_and_a_speaking_summoner_keep_the_run_waiting_silently(monkeypatch):
  _with_broker(monkeypatch)
  monkeypatch.setattr(turn_end.summon, 'summoned', lambda: True)
  monkeypatch.setattr(turn_end.summon, 'talk', lambda: ('owner.say',))
  store = FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)])
  working = FakePort(store, (_work('job-1'),))
  speaking = FakePort(store)

  turn_end.settle(working)
  turn_end.settle(speaking)

  assert (working.notifications, working.ends) == ([], 0)
  assert (speaking.notifications, speaking.ends) == ([], 0)


def test_a_failed_waiter_notices_even_beside_live_work(monkeypatch):
  _with_broker(monkeypatch, _mission('WEB-1', 'webview'))
  monkeypatch.setattr(turn_end.summon, 'summoned', lambda: True)
  store = FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND)])
  port = FakePort(store, (_work('job-1'),))

  turn_end.settle(port)
  turn_end.settle(port)

  [notice] = port.notifications
  assert 'mission WEB-1: webview' in notice
  assert 'orphans' not in notice
  assert (
    'Deliver the result of your task through `bro::answer` once the whole task is done.' in notice
  )
  assert port.ends == 0


def test_a_stalled_watch_notices_once_and_no_longer_holds_the_run():
  stalled = FakeWatch('poll-pr x', stalled=True)
  port = FakePort(FakeStore([stalled]))

  turn_end.settle(port)
  assert 'the watch `poll-pr x` is alive but has stopped beating' in port.notifications[0]
  assert port.ends == 0

  turn_end.settle(port)
  assert len(port.notifications) == 1
  assert port.ends == 1


def test_a_stalled_session_watch_is_a_down_session_watch(monkeypatch):
  monkeypatch.setattr(watches, 'session_watch_admitted', lambda: True)
  _with_broker(monkeypatch, _mission('Q1'))
  port = FakePort(FakeStore([FakeWatch(watches.SESSION_WATCH_COMMAND, stalled=True)]))

  turn_end.settle(port)

  [notice] = port.notifications
  assert 'the session watch `quest watch` is down' in notice
  assert 'quest Q1 to dev' in notice


def test_uncovered_mission_notifies_once_then_ends(monkeypatch):
  _with_broker(monkeypatch, _mission('Q1'))
  port = FakePort(FakeStore())

  turn_end.settle(port)
  turn_end.settle(port)

  assert len(port.notifications) == 1
  assert 'quest Q1 to dev' in port.notifications[0]
  assert '`bro::quest_cancel`' in port.notifications[0]
  assert 'orphans the uncovered work' in port.notifications[0]
  assert 'bro::answer' not in port.notifications[0]
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
  assert '[watch-run] exited 1' in port.notifications[0]
  assert port.ends == 1


def test_undelivered_exit_line_precedes_dead_watch_notice(monkeypatch):
  monkeypatch.setattr(watches, 'session_watch_admitted', lambda: True)
  store = FakeStore(
    [FakeWatch(watches.SESSION_WATCH_COMMAND, alive=False, last='[watch-run] exited 1')],
    waking=True,
  )
  port = FakePort(store)

  turn_end.settle(port)
  assert port.notifications == []
  assert port.ends == 0

  store.waking = False
  turn_end.settle(port)
  assert len(port.notifications) == 1


def test_a_beat_refreshed_between_probes_cannot_end_a_run_with_a_healthy_watch():
  stalled = watches.BeatSnapshot('old beat', 1.0)
  fresh = watches.BeatSnapshot('new beat', time.time() + 1000)
  watch = FakeWatch('poll-pr x', snapshots=[stalled, fresh])
  port = FakePort(FakeStore([watch]))

  turn_end.settle(port)

  assert port.ends == 0
  assert 'poll-pr x' in port.notifications[0]
  assert cast(FakeStore, port.watch_store).reported == [('poll-pr x', 'old beat')]


def test_the_beat_a_verdict_judged_is_the_one_it_reports(monkeypatch):
  stalled = watches.BeatSnapshot('old beat', 1.0)
  fresh = watches.BeatSnapshot('new beat', time.time() + 1000)
  watch = FakeWatch('poll-pr x', snapshots=[stalled, fresh])
  store = FakeStore([watch])
  port = FakePort(store)

  assert turn_end.failure_notice(port) is not None

  assert store.reported == [('poll-pr x', 'old beat')]
  assert watch.beat_snapshot() == fresh


def test_the_liveness_expiry_is_the_nearest_beat_deadline_on_the_monotonic_clock(monkeypatch):
  monkeypatch.setattr(turn_end.time, 'time', lambda: 1000.0)
  monkeypatch.setattr(turn_end.time, 'monotonic', lambda: 50.0)
  watch = FakeWatch('poll-pr x', deadline=1030.0)
  port = FakePort(FakeStore([watch, FakeWatch('other', deadline=1090.0), FakeWatch('plain')]))

  assert turn_end.next_liveness_expiry(port) == 80.0
  assert turn_end.next_liveness_expiry(FakePort(FakeStore([FakeWatch('plain')]))) is None


def test_a_beat_that_stalls_after_the_session_went_idle_draws_its_notice_once():
  watch = FakeWatch('poll-pr x')
  port = FakePort(FakeStore([watch]))
  turn_end.settle(port)
  assert (port.notifications, port.ends) == ([], 0)

  watch.stalled = True

  notice = turn_end.failure_notice(port)
  assert notice is not None and 'the watch `poll-pr x` is alive but has stopped beating' in notice
  assert turn_end.failure_notice(port) is None
  assert cast(FakeStore, port.watch_store).reported == [
    ('poll-pr x', 'beat 1'),
    ('poll-pr x', 'beat 1'),
  ]


def test_a_reported_session_watch_stall_leaves_the_deadline_and_stays_noticed_once(monkeypatch):
  monkeypatch.setattr(watches, 'session_watch_admitted', lambda: True)
  _with_broker(monkeypatch)
  watch = FakeWatch(watches.SESSION_WATCH_COMMAND, stalled=True)
  port = FakePort(FakeStore([watch]))

  first = turn_end.failure_notice(port)

  assert first is not None and 'the session watch `quest watch` is down' in first
  assert cast(FakeStore, port.watch_store).reported == [(watches.SESSION_WATCH_COMMAND, 'beat 1')]
  assert turn_end.failure_notice(port) is None


def test_silence_notices_follow_the_patience_and_its_backoff():
  port = FakePort(FakeStore(), (_work('job-1', last_active=100.0),))

  assert turn_end.silence_notice(port, 159.0) is None
  assert turn_end.next_silence_expiry(port) == 160.0

  notice = turn_end.silence_notice(port, 160.0)
  assert notice is not None
  assert 'job-1 bg `sleep` silent for 1m; next notice in 2m' in notice
  assert turn_end.silence_notice(port, 279.0) is None
  assert turn_end.next_silence_expiry(port) == 280.0

  assert turn_end.silence_notice(port, 280.0) is not None
  assert turn_end.next_silence_expiry(port) == 520.0
  assert 'next notice in 4m' in (turn_end.silence_notice(port, 520.0) or '')
  assert turn_end.next_silence_expiry(port) == 760.0


def test_activity_restarts_the_silence_from_the_base_patience():
  port = FakePort(FakeStore(), (_work('job-1', last_active=0.0),))
  assert turn_end.silence_notice(port, 60.0) is not None

  port.background = (_work('job-1', last_active=100.0),)

  assert turn_end.silence_notice(port, 159.0) is None
  assert turn_end.next_silence_expiry(port) == 160.0
  assert 'next notice in 2m' in (turn_end.silence_notice(port, 160.0) or '')


def test_silence_is_judged_per_job_and_a_finished_job_is_forgotten():
  port = FakePort(FakeStore(), (_work('job-1', 0.0), _work('job-2', 50.0)))

  notice = turn_end.silence_notice(port, 60.0)

  assert notice is not None
  assert 'job-1' in notice and 'job-2' not in notice
  port.background = ()
  assert turn_end.next_silence_expiry(port) is None
  port.background = (_work('job-1', 0.0),)
  assert 'job-1' in (turn_end.silence_notice(port, 70.0) or '')


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

  assert (port.notifications, port.ends) == ([], 0)


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

    monkeypatch.setattr(turn_end.mission, 'visible_event_head', head_read_before_the_reply)
    await asyncio.to_thread(turn_end.settle, port)

  assert (port.notifications, port.ends) == ([], 0)


def _answer(text: str) -> None:
  lifecycle = RunLifecycle.from_env()
  assert lifecycle is not None
  lifecycle.completed(text, 'ok')
  lifecycle.close()


def _ended(quest_id: str) -> None:
  with mission.open_client() as client:
    record = mission.query_mission(client, quest_id, wait_seconds=TIMEOUT)
  assert record['state'] == 'ended', record


def _journal_head() -> int:
  with mission.open_client() as client:
    return mission._event_head(client)


@pytest.mark.asyncio
async def test_the_sessions_own_end_is_no_event_its_turn_end_waits_for(monkeypatch):
  # the session watch armed at the journal head, as `quest watch` does, and the
  # end the session's answer journals is its summoner's to see
  watch = FakeWatch(watches.SESSION_WATCH_COMMAND)
  port = FakePort(FakeStore([watch]))
  async with _summoned_session(monkeypatch, ('worker.say',)) as (_, session):
    watch.head = await asyncio.to_thread(_journal_head)
    await asyncio.to_thread(_answer, 'done')
    await asyncio.to_thread(_ended, session.quest)

    await asyncio.to_thread(turn_end.settle, port)

  assert cast(FakeStore, port.watch_store).waited_for == []
  assert port.ends == 1
