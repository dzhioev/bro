"""The shared turn-end verdict, the background-work silence clock, and the harness ports."""

import os
import time
from dataclasses import dataclass
from typing import Protocol

from bro import mission, quest, summon, watches
from bro.broker.environment import BROKER_CHANNEL

PATIENCE_SECONDS = 15 * 60.0
PATIENCE_CAP_SECONDS = 4 * 60 * 60.0


class LineSink(Protocol):
  """A harness destination for one bounded batch of watch lines."""

  def deliver(self, batch: str) -> None: ...


@dataclass(frozen=True)
class BackgroundWork:
  """One piece of harness background work and when, on the monotonic clock, it last showed life."""

  id: str
  label: str
  last_active: float


@dataclass
class _Patience:
  seen_active: float
  anchor: float
  seconds: float


class SilenceClock:
  """How long each piece of background work may stay silent before its next notice.

  The first notice comes `patience` after the work's last activity; each
  notice doubles the wait up to `cap`, counted from the notice, and any
  activity starts over from `patience`."""

  def __init__(self, patience: float | None = None, cap: float | None = None):
    self._patience = PATIENCE_SECONDS if patience is None else patience
    self._cap = PATIENCE_CAP_SECONDS if cap is None else cap
    self._entries: dict[str, _Patience] = {}

  def _entry(self, work: BackgroundWork) -> _Patience:
    entry = self._entries.get(work.id)
    if entry is None or work.last_active > entry.seen_active:
      entry = _Patience(work.last_active, work.last_active, self._patience)
      self._entries[work.id] = entry
    return entry

  def _retain(self, works: tuple[BackgroundWork, ...]) -> None:
    live = {work.id for work in works}
    for work_id in [key for key in self._entries if key not in live]:
      del self._entries[work_id]

  def due(self, works: tuple[BackgroundWork, ...], now: float) -> list[BackgroundWork]:
    self._retain(works)
    return [work for work in works if now >= (entry := self._entry(work)).anchor + entry.seconds]

  def notified(self, works: list[BackgroundWork], now: float) -> None:
    for work in works:
      entry = self._entry(work)
      entry.anchor = now
      entry.seconds = min(entry.seconds * 2, self._cap)

  def next_expiry(self, works: tuple[BackgroundWork, ...]) -> float | None:
    self._retain(works)
    expiries = [(entry := self._entry(work)).anchor + entry.seconds for work in works]
    return min(expiries) if len(expiries) > 0 else None

  def seconds_after_notice(self, work: BackgroundWork) -> float:
    return self._entry(work).seconds


class TurnEnd(Protocol):
  """The harness operations and watch store used to settle a one-shot turn."""

  @property
  def watch_store(self) -> watches.Store: ...

  @property
  def silence_clock(self) -> SilenceClock: ...

  def background_work(self) -> tuple[BackgroundWork, ...]: ...

  def notify(self, text: str) -> None: ...

  def end(self) -> None: ...


@dataclass(frozen=True)
class LiveWork:
  missions: tuple[mission.LiveMission, ...]
  covered_mission_ids: frozenset[str]
  model_watches: tuple[str, ...]
  stalled_watches: tuple[tuple[str, watches.BeatSnapshot], ...]
  waking_lines: bool
  background_work: tuple[BackgroundWork, ...]
  summoner_can_speak: bool
  reply_awaited: bool
  session_journal_head: int | None
  session_watch_lagging: bool
  session_watch_down: bool
  session_watch_stall: watches.BeatSnapshot | None
  session_watch_exit: str | None

  @property
  def covered_missions(self) -> tuple[mission.LiveMission, ...]:
    return tuple(item for item in self.missions if item.mission_id in self.covered_mission_ids)

  @property
  def uncovered_missions(self) -> tuple[mission.LiveMission, ...]:
    return tuple(item for item in self.missions if item.mission_id not in self.covered_mission_ids)

  @property
  def live(self) -> bool:
    return (
      len(self.covered_missions) > 0
      or len(self.model_watches) > 0
      or len(self.background_work) > 0
      or self.summoner_can_speak
      or self.reply_awaited
    )

  def _session_stall_text(self) -> str:
    return '' if self.session_watch_stall is None else self.session_watch_stall.text.strip()

  def failure_key(self) -> frozenset[str]:
    values = [
      *(f'mission:{item.mission_id}' for item in self.uncovered_missions),
      *(f'stalled:{command}:{beat.text.strip()}' for command, beat in self.stalled_watches),
    ]
    if self.session_watch_down:
      values.append(f'session-watch-down:{self.session_watch_exit}:{self._session_stall_text()}')
    return frozenset(values)


def _reply_awaited() -> bool:
  with mission.open_client() as client:
    record = mission.query_mission(client, mission.own_mission())
  return len(quest.open_questions(record, awaiting='owner')) > 0


def _live_work(port: TurnEnd) -> LiveWork:
  store = port.watch_store
  now = time.time()
  declared = store.declared()
  session_watch = next(
    (watch for watch in declared if watch.command == watches.SESSION_WATCH_COMMAND), None
  )
  session_beat = (
    session_watch.beat_snapshot()
    if session_watch is not None and session_watch.producer_alive()
    else None
  )
  session_watch_stall = (
    session_beat if session_beat is not None and now > session_beat.deadline else None
  )
  session_watch_alive = (
    session_watch is not None and session_watch.producer_alive() and session_watch_stall is None
  )
  model_watches: list[str] = []
  stalled_watches: list[tuple[str, watches.BeatSnapshot]] = []
  for watch in declared:
    if watch.command == watches.SESSION_WATCH_COMMAND or not watch.producer_alive():
      continue
    beat = watch.beat_snapshot()
    if beat is not None and now > beat.deadline:
      stalled_watches.append((watch.command, beat))
    else:
      model_watches.append(watch.command)

  has_broker = os.environ.get(BROKER_CHANNEL) is not None
  missions = tuple(mission.live_missions()) if has_broker else ()
  reply_awaited = session_watch_alive and has_broker and _reply_awaited()
  # read after every broker state read above, so whatever changed since them
  # that this session can see is an event the session watch must publish
  # before a verdict
  journal_head = mission.visible_event_head() if has_broker and session_watch_alive else None
  published_head = (
    session_watch.journal_head() if session_watch is not None and session_watch_alive else None
  )
  session_watch_lagging = journal_head is not None and (
    published_head is None or published_head < journal_head
  )
  covered = {item.mission_id for item in missions if session_watch_alive}

  session_watch_expected = watches.session_watch_admitted()
  session_watch_down = session_watch_expected and not session_watch_alive
  talk = summon.talk() or ()
  summoner_can_speak = (
    session_watch_alive
    and summon.summoned()
    and any(right in talk for right in ('owner.say', 'owner.question'))
  )
  return LiveWork(
    missions,
    frozenset(covered),
    tuple(model_watches),
    tuple(stalled_watches),
    store.has_waking_lines(),
    port.background_work(),
    summoner_can_speak,
    reply_awaited,
    journal_head,
    session_watch_lagging,
    session_watch_down,
    session_watch_stall,
    session_watch.last_complete_line()
    if session_watch_down and session_watch is not None
    else None,
  )


def _duration(seconds: float) -> str:
  minutes = max(1, round(seconds / 60))
  hours, remainder = divmod(minutes, 60)
  if hours == 0:
    return f'{minutes}m'
  return f'{hours}h' if remainder == 0 else f'{hours}h{remainder}m'


def silence_notice(port: TurnEnd, now: float) -> str | None:
  """The notice for background work silent past its patience, or None.

  Reports each piece of work due once and starts its next, longer wait."""
  works = port.background_work()
  clock = port.silence_clock
  due = clock.due(works, now)
  if len(due) == 0:
    return None
  clock.notified(due, now)
  lines = ['[notification: background work has been silent]']
  for work in due:
    lines.append(
      f'background work: {work.label} silent for {_duration(now - work.last_active)};'
      f' next notice in {_duration(clock.seconds_after_notice(work))} if it stays silent'
    )
  lines.append('Read its output or stop it, or end the turn to keep waiting.')
  return '\n'.join(lines)


def next_silence_expiry(port: TurnEnd) -> float | None:
  """The monotonic time background work next falls due a silence notice, or None."""
  return port.silence_clock.next_expiry(port.background_work())


def _failure_notice(work: LiveWork) -> str:
  lines = ['[notification: the turn ended with failed work]']
  lines.extend(mission.live_mission_line(item) for item in work.uncovered_missions)
  lines.extend(
    f'the watch `{command}` is alive but has stopped beating;'
    ' stop it with `bro::unwatch` or end the turn to keep waiting'
    for command, _ in work.stalled_watches
  )
  if work.session_watch_down:
    detail = (
      f': {work.session_watch_exit}'
      if work.session_watch_exit is not None
      else ' without a retained exit line'
    )
    lines.append(f'the session watch `{watches.SESSION_WATCH_COMMAND}` is down{detail}')
  if any(item.type == mission.BRO for item in work.uncovered_missions):
    lines.append('Use `bro::quest_cancel` to cancel an uncovered quest.')
  if not work.live:
    lines.append('Ending the turn again ends the run and orphans the uncovered work.')
  if summon.summoned():
    lines.append(
      'Deliver the result of your task through `bro::answer` once the whole task is done.'
    )
  return '\n'.join(lines)


def _unnoticed_failure(port: TurnEnd, work: LiveWork) -> str | None:
  for command, beat in work.stalled_watches:
    port.watch_store.mark_stall_reported(command, beat)
  if work.session_watch_stall is not None:
    port.watch_store.mark_stall_reported(watches.SESSION_WATCH_COMMAND, work.session_watch_stall)
  failure = work.failure_key()
  if work.waking_lines or len(failure) == 0 or not port.watch_store.mark_notified(failure):
    return None
  return _failure_notice(work)


def failure_notice(port: TurnEnd) -> str | None:
  """The notice for failed work not yet noticed in this distinct set, or None."""
  return _unnoticed_failure(port, _live_work(port))


def next_liveness_expiry(port: TurnEnd) -> float | None:
  """The monotonic time a beating watch whose stall is unreported next stalls at, or None;
  already past for a stall not yet reported."""
  deadline = port.watch_store.next_beat_deadline()
  if deadline is None:
    return None
  return time.monotonic() + (deadline - time.time())


def settle(port: TurnEnd) -> None:
  """Apply the verdict to the current live work: end the run when none can
  report, notify once of failed work, and otherwise keep waiting."""
  work = _live_work(port)
  while work.session_watch_lagging:
    assert work.session_journal_head is not None
    port.watch_store.wait_for_journal_head(watches.SESSION_WATCH_COMMAND, work.session_journal_head)
    work = _live_work(port)
  if work.waking_lines:
    return
  notice = _unnoticed_failure(port, work)
  if notice is not None:
    port.notify(notice)
  elif not work.live:
    port.end()
