"""The broker journal: quest records, ordered events, and permanent lineage."""

import asyncio
import json
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from bro.base import log
from bro.base.time_util import Moment, utc_now
from bro.broker.runtime import Peer

MAX_RECORDS = 256
MAX_RESULT_BYTES = 2 << 20
MAX_EVENTS = 4096
MAX_EVENT_BATCH = 256
MAX_WAIT_SECONDS = 600.0
ARGS_STRING_HEAD = 160
ARGS_HEAD_BYTES = 2048


@dataclass
class Record:
  quest_id: str
  kind: str
  parent: Optional[str]
  requester: Optional[Peer]
  args: dict[str, Any]
  worker: Optional[Peer] = None
  state: str = 'accepted'
  accepted_at: Optional[Moment] = None
  started_at: Optional[Moment] = None
  ended_at: Optional[Moment] = None
  outcome: Optional[str] = None
  reason: Optional[str] = None
  trail_id: Optional[str] = None
  result: Optional[dict[str, Any]] = None
  result_evicted: bool = False
  order: int = field(default=0, repr=False)

  @property
  def terminal(self) -> bool:
    return self.state in ('ended', 'denied')

  def view(self, *, include_result: bool = False) -> dict[str, Any]:
    view: dict[str, Any] = {
      'id': self.quest_id,
      'kind': self.kind,
      'parent': self.parent,
      'args': self.args,
      'state': self.state,
    }
    for name in ('accepted_at', 'started_at', 'ended_at'):
      value = getattr(self, name)
      if value is not None:
        view[name] = value.isoformat()
    for name in ('outcome', 'reason', 'trail_id'):
      value = getattr(self, name)
      if value is not None:
        view[name] = value
    if include_result and self.result is not None:
      view['result'] = self.result
    if include_result and self.result_evicted:
      view['result_evicted'] = True
    return view


@dataclass(frozen=True)
class Event:
  seq: int
  at: Moment
  quest: str
  kind: str
  parent: Optional[str]
  transition: str
  payload: dict[str, Any]

  def view(self) -> dict[str, Any]:
    return {
      'seq': self.seq,
      'at': self.at.isoformat(),
      'quest': self.quest,
      'kind': self.kind,
      'parent': self.parent,
      'transition': self.transition,
      **self.payload,
    }


@dataclass(frozen=True)
class Lineage:
  parent: Optional[str]
  kind: str


Subscriber = Callable[[Event, Record], None]


class Journal:
  def __init__(self):
    self.records: OrderedDict[str, Record] = OrderedDict()
    self.lineage: dict[str, Lineage] = {}
    self._events: deque[Event] = deque(maxlen=MAX_EVENTS)
    self._subscribers: list[Subscriber] = []
    self._seq = 0
    self._order = 0
    self._result_bytes = 0
    self._changed = asyncio.Event()

  @property
  def head(self) -> int:
    return self._seq

  @property
  def first_event_seq(self) -> Optional[int]:
    return self._events[0].seq if len(self._events) > 0 else None

  def subscribe(self, subscriber: Subscriber) -> None:
    self._subscribers.append(subscriber)

  def knows(self, quest_id: str) -> bool:
    return quest_id in self.lineage

  def open(
    self,
    quest_id: str,
    kind: str,
    parent: Optional[str],
    requester: Optional[Peer],
    args: dict[str, Any],
  ) -> Record:
    if quest_id in self.lineage:
      raise ValueError(f'quest id {quest_id!r} already exists')
    now = utc_now()
    record = Record(
      quest_id=quest_id,
      kind=kind,
      parent=parent,
      requester=requester,
      args=bounded_args(args),
      accepted_at=now,
      order=self._next_order(),
    )
    self.records[quest_id] = record
    self.lineage[quest_id] = Lineage(parent, kind)
    self._append(record, 'accepted', {}, at=now)
    self._retain()
    return record

  def deny(
    self,
    quest_id: str,
    kind: str,
    parent: Optional[str],
    requester: Peer,
    args: dict[str, Any],
    reason: str,
  ) -> Record:
    if quest_id in self.lineage:
      raise ValueError(f'quest id {quest_id!r} already exists')
    now = utc_now()
    result = {'outcome': 'denied', 'error': reason}
    record = Record(
      quest_id=quest_id,
      kind=kind,
      parent=parent,
      requester=requester,
      args=bounded_args(args),
      state='denied',
      ended_at=now,
      outcome='denied',
      reason=reason,
      result=result,
      order=self._next_order(),
    )
    self._result_bytes += _payload_bytes(result)
    self.records[quest_id] = record
    self.lineage[quest_id] = Lineage(parent, kind)
    self._append(record, 'denied', {'reason': reason}, at=now)
    self._retain()
    return record

  def bind(self, record: Record, worker: Peer) -> None:
    self._require_current(record)
    if record.worker is not None:
      raise RuntimeError(f'quest {record.quest_id} already has a worker')
    record.worker = worker

  def started(self, record: Record) -> bool:
    self._require_live(record)
    if record.started_at is not None:
      return False
    record.started_at = utc_now()
    record.state = 'started'
    self._append(record, 'started', {}, at=record.started_at)
    return True

  def trail(self, record: Record, trail_id: str) -> bool:
    self._require_live(record)
    if record.trail_id is not None:
      return False
    record.trail_id = trail_id
    self._append(record, 'trail', {'trail_id': trail_id})
    return True

  def end(
    self,
    record: Record,
    result: dict[str, Any],
    *,
    outcome: Optional[str] = None,
    reason: Optional[str] = None,
  ) -> None:
    self._require_live(record)
    record.state = 'ended'
    record.ended_at = utc_now()
    record.order = self._next_order()
    record.result = result
    record.outcome = outcome if outcome is not None else str(result.get('outcome'))
    detail = result.get('detail')
    result_reason = detail.get('reason') if isinstance(detail, dict) else None
    record.reason = reason if reason is not None else result_reason
    self._result_bytes += _payload_bytes(result)
    event_payload: dict[str, Any] = {'outcome': record.outcome}
    if record.reason is not None:
      event_payload['reason'] = record.reason
    self._append(record, 'ended', event_payload, at=record.ended_at)
    self._retain()

  def evicted_view(self, quest_id: str) -> Optional[dict[str, Any]]:
    lineage = self.lineage.get(quest_id)
    if lineage is None:
      return None
    return {
      'id': quest_id,
      'kind': lineage.kind,
      'parent': lineage.parent,
      'state': 'evicted',
    }

  def ancestry(self, quest_id: str) -> tuple[str, ...]:
    """The quest's ancestors, nearest parent first."""
    ancestors = []
    lineage = self.lineage.get(quest_id)
    if lineage is None:
      raise ValueError(f'unknown quest id {quest_id!r}')
    parent = lineage.parent
    while parent is not None:
      ancestors.append(parent)
      lineage = self.lineage.get(parent)
      if lineage is None:
        raise RuntimeError(f'quest {quest_id!r} has unknown ancestor {parent!r}')
      parent = lineage.parent
    return tuple(ancestors)

  def visible(self, caller: Peer, record: Record, workers: dict[Peer, str]) -> bool:
    caller_quest = workers.get(caller)
    if caller_quest is None:
      return False
    current = record.parent
    while current is not None:
      if current == caller_quest:
        return True
      lineage = self.lineage.get(current)
      current = lineage.parent if lineage is not None else None
    return False

  def visible_records(self, caller: Peer, workers: dict[Peer, str]) -> list[Record]:
    records = [record for record in self.records.values() if self.visible(caller, record, workers)]
    records.sort(key=listing_position)
    return records

  def views(self, caller: Peer, workers: dict[Peer, str]) -> list[dict[str, Any]]:
    return [record.view() for record in self.visible_records(caller, workers)]

  def events_after(
    self, after: int, caller: Peer, workers: dict[Peer, str]
  ) -> tuple[int, list[dict[str, Any]]]:
    first = self.first_event_seq
    if after > 0 and first is not None and after < first - 1:
      raise ValueError(f'events gap: retained from seq {first}; re-baseline with query')
    visible = []
    for event in self._events:
      if event.seq <= after:
        continue
      record = self.records.get(event.quest)
      if record is None:
        lineage = self.lineage[event.quest]
        probe = Record(event.quest, lineage.kind, lineage.parent, None, {})
      else:
        probe = record
      if self.visible(caller, probe, workers):
        visible.append(event.view())
      if len(visible) == MAX_EVENT_BATCH:
        break
    return self.head, visible

  def change_event(self) -> asyncio.Event:
    return self._changed

  def _append(
    self,
    record: Record,
    transition: str,
    payload: dict[str, Any],
    *,
    at: Optional[Moment] = None,
  ) -> Event:
    self._seq += 1
    event = Event(
      self._seq,
      at if at is not None else utc_now(),
      record.quest_id,
      record.kind,
      record.parent,
      transition,
      payload,
    )
    self._events.append(event)
    changed = self._changed
    self._changed = asyncio.Event()
    changed.set()
    for subscriber in self._subscribers:
      try:
        subscriber(event, record)
      except Exception as error:
        log.warning('broker journal subscriber failed: %r', error)
    return event

  def _retain(self) -> None:
    while self._result_bytes > MAX_RESULT_BYTES:
      record = min(
        (
          candidate
          for candidate in self.records.values()
          if candidate.terminal and candidate.result is not None
        ),
        key=lambda candidate: candidate.order,
        default=None,
      )
      if record is None:
        break
      payload = record.result
      assert payload is not None
      self._result_bytes -= _payload_bytes(payload)
      record.result = None
      record.result_evicted = True
    while len(self.records) > MAX_RECORDS:
      record = min(
        (candidate for candidate in self.records.values() if candidate.terminal),
        key=lambda candidate: candidate.order,
        default=None,
      )
      if record is None:
        break
      self.records.pop(record.quest_id)
      if record.result is not None:
        self._result_bytes -= _payload_bytes(record.result)

  def _next_order(self) -> int:
    self._order += 1
    return self._order

  def _require_current(self, record: Record) -> None:
    if self.records.get(record.quest_id) is not record:
      raise RuntimeError(f'quest {record.quest_id} is not retained')

  def _require_live(self, record: Record) -> None:
    self._require_current(record)
    if record.terminal:
      raise RuntimeError(f'quest {record.quest_id} is already terminal')


def listing_position(record: Record) -> tuple[bool, int, str]:
  return record.terminal, -record.order, record.quest_id


def bounded_args(args: dict[str, Any]) -> dict[str, Any]:
  value = _bounded_value(args)
  if not isinstance(value, dict):
    raise TypeError('request args must be a dict')
  if _payload_bytes(value) <= ARGS_HEAD_BYTES:
    return value
  encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
  head = encoded[:ARGS_HEAD_BYTES]
  bounded = {'head': head, 'truncated': True}
  while _payload_bytes(bounded) > ARGS_HEAD_BYTES:
    overflow = _payload_bytes(bounded) - ARGS_HEAD_BYTES
    head = head[: max(0, len(head) - overflow)]
    bounded['head'] = head
  return bounded


def _bounded_value(value: Any) -> Any:
  if isinstance(value, str):
    return value[:ARGS_STRING_HEAD]
  if isinstance(value, dict):
    if not all(isinstance(key, str) for key in value):
      raise TypeError('request arg object keys must be strings')
    return {key: _bounded_value(inner) for key, inner in value.items()}
  if isinstance(value, list):
    return [_bounded_value(inner) for inner in value]
  if value is None or isinstance(value, (bool, int, float)):
    return value
  raise TypeError(f'unsupported request arg value: {type(value).__name__}')


def _payload_bytes(payload: dict[str, Any]) -> int:
  return len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())
