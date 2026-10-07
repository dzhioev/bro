"""The shared one-shot turn-end decision and its harness ports."""

import os
from dataclasses import dataclass
from typing import Protocol

from bro import mission, quest, summon, watches
from bro.broker.environment import BROKER_CHANNEL


class LineSink(Protocol):
  """A harness destination for one bounded batch of watch lines."""

  def deliver(self, batch: str) -> None: ...


class TurnEnd(Protocol):
  """The harness operations and watch store used to settle a one-shot turn."""

  @property
  def watch_store(self) -> watches.Store: ...

  def background_work(self) -> tuple[str, ...]: ...

  def notify(self, text: str) -> None: ...

  def end(self) -> None: ...


@dataclass(frozen=True)
class LiveWork:
  missions: tuple[mission.LiveMission, ...]
  covered_mission_ids: frozenset[str]
  model_watches: tuple[str, ...]
  pending_lines: bool
  background_work: tuple[str, ...]
  summoner_can_speak: bool
  reply_awaited: bool
  session_journal_head: int | None
  session_watch_lagging: bool
  session_watch_down: bool
  session_watch_exit: str | None

  @property
  def covered_missions(self) -> tuple[mission.LiveMission, ...]:
    return tuple(item for item in self.missions if item.mission_id in self.covered_mission_ids)

  @property
  def uncovered_missions(self) -> tuple[mission.LiveMission, ...]:
    return tuple(item for item in self.missions if item.mission_id not in self.covered_mission_ids)

  def key(self) -> frozenset[str]:
    values = [
      *(f'mission:{item.mission_id}' for item in self.missions),
      *(f'covered:{mission_id}' for mission_id in self.covered_mission_ids),
      *(f'watch:{command}' for command in self.model_watches),
      *(f'background:{item}' for item in self.background_work),
    ]
    if self.pending_lines:
      values.append('pending-lines')
    if self.summoner_can_speak:
      values.append('summoner-can-speak')
    if self.reply_awaited:
      values.append('reply-awaited')
    if self.session_watch_down:
      values.append(f'session-watch-down:{self.session_watch_exit}')
    return frozenset(values)


def _reply_awaited() -> bool:
  with mission.open_client() as client:
    record = mission.query_mission(client, mission.own_mission())
  return len(quest.open_questions(record, awaiting='owner')) > 0


def _live_work(port: TurnEnd) -> LiveWork:
  store = port.watch_store
  declared = store.declared()
  session_watch = next(
    (watch for watch in declared if watch.command == watches.SESSION_WATCH_COMMAND), None
  )
  session_watch_alive = session_watch is not None and session_watch.producer_alive()
  model_watches = tuple(
    watch.command
    for watch in declared
    if watch.command != watches.SESSION_WATCH_COMMAND and watch.producer_alive()
  )

  has_broker = os.environ.get(BROKER_CHANNEL) is not None
  missions = tuple(mission.live_missions()) if has_broker else ()
  reply_awaited = session_watch_alive and has_broker and _reply_awaited()
  # read after every broker state read above, so whatever changed since them
  # is an event the session watch must publish before a verdict
  journal_head = mission.event_head() if has_broker and session_watch_alive else None
  published_head = (
    session_watch.journal_head() if session_watch is not None and session_watch_alive else None
  )
  session_watch_lagging = journal_head is not None and (
    published_head is None or published_head < journal_head
  )
  covered = {
    item.mission_id for item in missions if session_watch_alive and item.type == mission.BRO
  }

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
    model_watches,
    store.has_pending_lines(),
    port.background_work(),
    summoner_can_speak,
    reply_awaited,
    journal_head,
    session_watch_lagging,
    session_watch_down,
    session_watch.last_complete_line()
    if session_watch_down and session_watch is not None
    else None,
  )


def _notice(work: LiveWork, *, waits: bool) -> str:
  lines = ['[notification: the turn ended with work still live]']
  lines.extend(f'background work: {item}' for item in work.background_work)
  lines.extend(mission.live_mission_line(item) for item in work.uncovered_missions)
  if work.summoner_can_speak:
    lines.append('the summoner may still send a message')
  if work.session_watch_down:
    detail = (
      f': {work.session_watch_exit}'
      if work.session_watch_exit is not None
      else ' without a retained exit line'
    )
    lines.append(f'the session watch `{watches.SESSION_WATCH_COMMAND}` is down{detail}')
  if any(item.type == mission.BRO for item in work.uncovered_missions):
    lines.append('Use `bro::quest_cancel` to cancel an uncovered quest.')
  if waits:
    lines.append('Ending the turn again keeps waiting for this work.')
  else:
    lines.append('Ending the turn again ends the run and orphans the uncovered work.')
  if summon.summoned():
    lines.append('When the work is finished, deliver the result through `bro::answer`.')
  return '\n'.join(lines)


def settle(port: TurnEnd) -> None:
  """Apply the first matching one-shot verdict to the current live work."""
  work = _live_work(port)
  while work.session_watch_lagging:
    assert work.session_journal_head is not None
    port.watch_store.wait_for_journal_head(watches.SESSION_WATCH_COMMAND, work.session_journal_head)
    work = _live_work(port)
  if (
    len(work.covered_missions) > 0
    or len(work.model_watches) > 0
    or work.pending_lines
    or work.reply_awaited
  ):
    return

  if len(work.background_work) > 0 or work.summoner_can_speak:
    if port.watch_store.mark_notified(work.key()):
      port.notify(_notice(work, waits=True))
    return

  if len(work.uncovered_missions) > 0 or work.session_watch_down:
    if port.watch_store.mark_notified(work.key()):
      port.notify(_notice(work, waits=False))
      return

  port.end()
