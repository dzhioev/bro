from dataclasses import dataclass, field
from typing import cast

import pytest

from bro import mission, turn_end, watches
from bro.broker.environment import BROKER_CHANNEL


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
  monkeypatch.setattr(turn_end, '_reply_awaited', lambda: False)
  monkeypatch.setattr(turn_end.mission, 'event_head', lambda: 0)


def _mission(mission_id: str, worker_type: str = 'bro') -> mission.LiveMission:
  label = 'dev' if worker_type == 'bro' else worker_type
  return mission.LiveMission(mission_id, worker_type, label)


def _with_missions(monkeypatch, *missions: mission.LiveMission) -> None:
  monkeypatch.setenv(BROKER_CHANNEL, 'unused')
  monkeypatch.setattr(turn_end.mission, 'live_missions', lambda: list(missions))


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
  _with_missions(monkeypatch, *missions)
  monkeypatch.setattr(turn_end, '_reply_awaited', lambda: reply_awaited)
  port = FakePort(store, ('task-1',))

  turn_end.settle(port)

  assert port.notifications == []
  assert port.ends == 0


def test_settlement_waits_for_the_session_watch_journal_head(monkeypatch):
  _with_missions(monkeypatch)
  monkeypatch.setattr(turn_end.mission, 'event_head', lambda: 7)
  watch = FakeWatch(watches.SESSION_WATCH_COMMAND, head=6)
  port = FakePort(FakeStore([watch]))

  turn_end.settle(port)

  assert port.ends == 1
  assert cast(FakeStore, port.watch_store).waited_for == [7]


def test_owner_led_background_work_notifies_then_keeps_waiting(monkeypatch):
  _with_missions(monkeypatch, _mission('WEB-1', 'webview'))
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
  _with_missions(monkeypatch, _mission('Q1'))
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
  monkeypatch.setattr(turn_end, '_reply_awaited', lambda: True)
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
