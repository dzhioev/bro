"""mission — read, talk to, and end a mission through the session broker.

A mission is work undertaken by any registered worker type. This module owns the
universal outcome and conversation reads, typed chat moves, caller-scoped listing,
ordered watch, and cancellation surfaces, as a library and as the ``mission`` CLI.
``self`` names the session's own mission.

Reads are repeatable journal queries. A wait bounds silence rather than the
mission: it re-reads the journal through bounded long-polls until the state,
chat advance, or supervision settlement it waits for, and a caller past the
deadline sees the state it stopped at. Watch replays retained chat through the head it arms at, then
long-polls the ordered event stream after it.

Unlike the substrate CLI, an unset ``BROKER_CHANNEL`` is an error.
Broker imports stay deferred so importing the constants does not pull in the
broker implementation on pre-gate launch paths.
"""

import contextlib
import json
import math
import os
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, cast

import bro.base.args as base_args
from bro.base import log

if TYPE_CHECKING:
  from bro.broker.brotocol import End, Message, Talk
  from bro.broker.client import Client

__cli_name__ = 'mission'

LAUNCH = 'launch'
BRO = 'bro'
SELF = 'self'  # the mission id naming the session's own mission — the one it answers
# acceptance and inline-read replies should arrive promptly; the bound turns a
# wedged broker into a clean client failure
ACCEPT_TIMEOUT = 30.0
# journal long-polls stay well inside harness-side MCP call budgets
READ_WAIT_SECONDS = 25.0
# exit codes beside 0 (done) and 1 (failure): the mission still runs, or a
# question awaits the caller
RUNNING_EXIT_CODE = 3
QUESTION_EXIT_CODE = 4
WATCH_LINE_BYTES = 1024
MISSION_ID_HELP = f"mission id; `{SELF}` names this session's own mission"


class MissionError(Exception):
  """a mission read or move that produced no usable result: the mission was denied,
  failed, or is no longer retained, a read came back malformed, or the move is
  forbidden. The message is the operator-facing reason."""


class _BrokerReadTimeout(MissionError):
  pass


def open_client() -> 'Client':
  """Open a channel client whose lifecycle the caller owns."""
  from bro.broker.client import Client
  from bro.broker.environment import BROKER_CHANNEL

  client = Client.from_env()
  if client is None:
    raise MissionError(
      f'no broker channel ({BROKER_CHANNEL} unset); missions need a session channel'
    )
  return client


@contextlib.contextmanager
def connection(client: Optional['Client']) -> Generator['Client']:
  """the channel client a call runs on: a caller-owned one passed through with
  its lifecycle left alone, or a fresh one closed on the way out."""
  if client is not None:
    yield client
    return
  with open_client() as owned:
    yield owned


def own_mission() -> str:
  """the id of the mission this session answers."""
  from bro.broker.environment import BROKER_MISSION

  value = os.environ.get(BROKER_MISSION)
  if value is None:
    raise MissionError(f'{BROKER_MISSION} is missing from the session environment')
  return value


def resolve(mission_id: str) -> str:
  """the journal id `mission_id` names: itself, or the session's own mission for `self`."""
  if mission_id == SELF:
    return own_mission()
  if len(mission_id) == 0:
    raise MissionError('mission id must be non-empty')
  return mission_id


def caller_end(mission: dict[str, Any], mission_id: str) -> 'End':
  """Which end of the mission this session is: worker on its own, or owner."""
  own = own_mission()
  if mission_id == own:
    return 'worker'
  if mission.get('parent') == own:
    return 'owner'
  raise MissionError(f'mission {mission_id!r} is not one this session owns or undertakes')


def trails_hint(trail_id: Optional[str]) -> str:
  if trail_id is not None:
    return f'inspect the run with `rewind show {trail_id}`'
  return 'the run has announced no trail'


def call_ok(client: 'Client', kind: str, args: dict[str, Any], *, timeout: float) -> dict[str, Any]:
  """Send one inline request and return its `ok` result payload."""
  try:
    result = client.call(kind, args, timeout)
  except TimeoutError:
    raise _BrokerReadTimeout(f'no reply to broker {kind!r} request within {timeout:.0f}s') from None
  except ConnectionError as error:
    raise MissionError(f'broker channel closed during {kind!r} request: {error}') from None
  payload = result.payload
  if payload.get('outcome') != 'ok':
    raise MissionError(str(payload.get('error', payload)))
  return payload


def _read_value(
  client: 'Client', kind: str, args: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
  value = call_ok(client, kind, args, timeout=timeout).get('value')
  if not isinstance(value, dict):
    raise MissionError(f'broker {kind!r} read returned a malformed value: {value!r}')
  return value


def query_mission(
  client: 'Client',
  mission_id: str,
  *,
  wait_seconds: float = 0,
  since: Optional[int] = None,
  wait_for_settlement: bool = False,
  read_timeout: Optional[float] = None,
) -> dict[str, Any]:
  """One by-id journal read, optionally long-polling for the end, settlement, or chat."""
  from bro.broker.dispatcher import QUERY

  args: dict[str, Any] = {'id': mission_id}
  if wait_seconds > 0:
    args['wait'] = wait_seconds
  if since is not None:
    args['since'] = since
  if wait_for_settlement:
    args['settled'] = True
  value = _read_value(
    client,
    QUERY,
    args,
    timeout=(
      max(ACCEPT_TIMEOUT, wait_seconds + ACCEPT_TIMEOUT) if read_timeout is None else read_timeout
    ),
  )
  mission = value.get('mission')
  if not isinstance(mission, dict):
    raise MissionError(f'query for {mission_id!r} returned no mission record')
  return mission


def _poll_mission(
  client: 'Client',
  mission_id: str,
  mission: dict[str, Any],
  *,
  deadline: Optional[float],
  done: Callable[[dict[str, Any]], bool],
  on_chat: bool,
) -> dict[str, Any]:
  """Re-read `mission` through bounded waits until `done` holds or `deadline` passes.

  With `on_chat`, a wait also ends when the mission's chat advances.
  Returns the last record read, so a caller past the deadline sees the state it stopped at."""
  while not done(mission):
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
      break
    poll_seconds = READ_WAIT_SECONDS if remaining is None else min(READ_WAIT_SECONDS, remaining)
    try:
      mission = query_mission(
        client,
        mission_id,
        wait_seconds=poll_seconds,
        since=_chat_seq(mission) if on_chat else None,
        read_timeout=remaining,
      )
    except _BrokerReadTimeout:
      if deadline is None or time.monotonic() < deadline:
        raise
      break
  return mission


def wait_deadline(wait: bool, timeout: Optional[float]) -> Optional[float]:
  """the monotonic deadline a bounded wait runs to, None for an unbounded one;
  a bound without a wait or a non-finite bound is an argument error."""
  if timeout is None:
    return None
  if not wait:
    raise ValueError('timeout only bounds a wait; a plain read never blocks')
  if not math.isfinite(timeout) or timeout <= 0:
    raise ValueError('timeout must be a finite positive number')
  return time.monotonic() + timeout


def _remaining(deadline: Optional[float]) -> Optional[float]:
  return None if deadline is None else deadline - time.monotonic()


def _mission_id(mission: dict[str, Any]) -> str:
  mission_id = mission.get('id')
  if not isinstance(mission_id, str):
    raise MissionError('mission query returned no mission id')
  return mission_id


def _chat_seq(mission: dict[str, Any]) -> int:
  chat_seq = mission.get('chat_seq', 0)
  if not isinstance(chat_seq, int) or isinstance(chat_seq, bool):
    raise MissionError('mission query returned a malformed chat sequence')
  return chat_seq


def _ended(mission: dict[str, Any]) -> bool:
  state = mission.get('state')
  if state in ('accepted', 'started'):
    return False
  if state in ('ended', 'denied', 'evicted'):
    return True
  raise MissionError(f'mission {mission.get("id")!r} has unknown state {state!r}')


def _require_launch(mission: dict[str, Any]) -> str:
  mission_id = _mission_id(mission)
  if mission.get('kind') != LAUNCH:
    raise MissionError(f'mission {mission_id!r} is not a worker launch')
  worker_type = mission.get('type')
  if isinstance(worker_type, str):
    return worker_type
  if mission.get('state') == 'denied':
    args = mission.get('args')
    requested_type = args.get('type') if isinstance(args, dict) else None
    return requested_type if isinstance(requested_type, str) else 'unknown'
  raise MissionError(f'mission {mission_id!r} has no worker type')


def _require_live(mission: dict[str, Any]) -> None:
  if _ended(mission):
    raise MissionError(f'mission {mission.get("id")!r} already ended')


def chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
  """Validate one typed chat payload against the journal's message bound."""
  from bro.broker.journal import oversized_message

  if not isinstance(payload, dict):
    raise ValueError('mission chat payload must be a JSON object')
  reason = oversized_message(payload)
  if reason is not None:
    raise MissionError(f'{reason}; mint an artifact and send the ref')
  return payload


def payload_argument(value: str) -> dict[str, Any]:
  def reject_constant(constant: str) -> None:
    raise ValueError(f'mission payload contains non-JSON value {constant}')

  try:
    payload = json.loads(value, parse_constant=reject_constant)
  except json.JSONDecodeError as error:
    raise ValueError(f'mission payload is not valid JSON: {error.msg}') from error
  if not isinstance(payload, dict):
    raise ValueError('mission payload must be a JSON object')
  return payload


def _talk_of(mission: dict[str, Any]) -> 'Talk':
  from bro.broker.brotocol import TALK_RIGHTS

  values = mission.get('talk')
  if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
    raise MissionError('mission query returned malformed talk rights')
  talk = frozenset(values)
  if len(talk) != len(values) or not talk.issubset(TALK_RIGHTS):
    raise MissionError('mission query returned invalid talk rights')
  return cast('Talk', talk)


def _entries(mission: dict[str, Any]) -> list[dict[str, Any]]:
  messages = mission.get('messages')
  if not isinstance(messages, list) or not all(isinstance(entry, dict) for entry in messages):
    raise MissionError('mission query returned a malformed conversation')
  return messages


@dataclass(frozen=True)
class Outcome:
  mission_id: str
  type: str
  result: Optional[dict[str, Any]] = None
  trail_id: Optional[str] = None

  @property
  def state(self) -> str:
    return 'completed' if self.result is not None else 'running'


def _outcome_of(mission: dict[str, Any], mission_id: str) -> Outcome:
  state = mission.get('state')
  trail_id = mission.get('trail_id')
  retained_trail = trail_id if isinstance(trail_id, str) else None
  if state == 'evicted' or mission.get('result_evicted') is True:
    raise MissionError(f'the mission result is no longer retained; {trails_hint(retained_trail)}')
  worker_type = _require_launch(mission)
  if state in ('accepted', 'started'):
    return Outcome(mission_id, worker_type, trail_id=retained_trail)
  if state not in ('ended', 'denied'):
    raise MissionError(f'mission {mission_id!r} has unknown state {state!r}')
  result = mission.get('result')
  if not isinstance(result, dict):
    raise MissionError(
      f'mission {mission_id!r} has no retained result; {trails_hint(retained_trail)}'
    )
  return Outcome(mission_id, worker_type, result, retained_trail)


def check(mission_id: str, *, client: Optional['Client'] = None) -> Outcome:
  """Read the retained result of an owned mission without consuming it."""
  resolved = resolve(mission_id)
  if resolved == own_mission():
    raise MissionError(
      'a session cannot check the mission it undertakes; the outcome is its own to give'
    )
  with connection(client) as connected:
    mission = query_mission(connected, resolved)
    caller_end(mission, resolved)
  return _outcome_of(mission, resolved)


def outcome_view(outcome: Outcome) -> dict[str, Any]:
  view: dict[str, Any] = {
    'state': outcome.state,
    'mission_id': outcome.mission_id,
    'type': outcome.type,
  }
  if outcome.trail_id is not None:
    view['trail_id'] = outcome.trail_id
  if outcome.result is not None:
    view.update(outcome.result)
  return view


@dataclass(frozen=True)
class History:
  mission_id: str
  talk: tuple[str, ...]
  messages: tuple[dict[str, Any], ...]
  truncated: bool = False


def _event_at_sequence(client: 'Client', mission_id: str, sequence: int) -> dict[str, Any]:
  from bro.broker.dispatcher import EVENTS

  if sequence <= 0:
    raise ValueError('history sequence must be positive')
  value = _read_value(client, EVENTS, {'after': sequence - 1}, timeout=ACCEPT_TIMEOUT)
  events = value.get('events')
  if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
    raise MissionError('events read returned malformed records')
  for event in events:
    if event.get('seq') == sequence and event.get('mission') == mission_id:
      if event.get('transition') not in ('message', 'refused'):
        raise MissionError(f'event {sequence} is not a mission chat entry')
      return event
  raise MissionError(f'mission {mission_id!r} has no retained chat entry at sequence {sequence}')


def history(
  mission_id: str,
  *,
  sequence: Optional[int] = None,
  client: Optional['Client'] = None,
) -> History:
  """Read a mission's retained conversation, or one full entry by sequence."""
  resolved = resolve(mission_id)
  with connection(client) as connected:
    mission = query_mission(connected, resolved)
    caller_end(mission, resolved)
    _require_launch(mission)
    messages = (
      (_event_at_sequence(connected, resolved, sequence),)
      if sequence is not None
      else tuple(_entries(mission))
    )
  truncated = mission.get('messages_truncated', False)
  if not isinstance(truncated, bool):
    raise MissionError('mission query returned a malformed conversation truncation marker')
  return History(resolved, tuple(sorted(_talk_of(mission))), messages, truncated)


def history_view(history: History) -> dict[str, Any]:
  view: dict[str, Any] = {
    'mission_id': history.mission_id,
    'talk': list(history.talk),
    'messages': list(history.messages),
  }
  if history.truncated:
    view['truncated'] = True
  return view


# --- say and ask ----------------------------------------------------------------


def _send(
  client: 'Client',
  mission_id: str,
  payload: dict[str, Any],
  *,
  reply_to: Optional[str],
  question: bool,
  validate: Optional[Callable[[dict[str, Any]], None]] = None,
) -> tuple[str, 'Message']:
  """Send one chat move after validating the mission and its talk rights."""
  from bro.broker import brotocol
  from bro.broker.environment import BROKER_TALK

  resolved = resolve(mission_id)
  mission = query_mission(client, resolved)
  sender = caller_end(mission, resolved)
  if validate is not None:
    validate(mission)
  _require_live(mission)
  try:
    candidate = brotocol.message(
      resolved,
      payload,
      id='question' if question else None,
      reply_to=reply_to,
    )
  except brotocol.ProtocolError as error:
    raise MissionError(str(error)) from error
  talk = _talk_of(mission)
  if sender == 'worker':
    from bro.broker.client import talk_from_env

    published_talk = talk_from_env()
    if published_talk is None:
      raise MissionError(f'{BROKER_TALK} is missing from the session environment')
    talk = published_talk
  if not brotocol.message_allowed(talk, sender, candidate):
    source = BROKER_TALK if sender == 'worker' else f'query {resolved}'
    raise MissionError(f'{source} forbids this mission chat move')
  try:
    sent = client.message(resolved, candidate.payload, reply_to=reply_to, question=question)
  except (PermissionError, brotocol.ProtocolError) as error:
    raise MissionError(str(error)) from error
  return resolved, sent


def say(
  mission_id: str,
  payload: dict[str, Any],
  *,
  reply_to: Optional[str] = None,
  client: Optional['Client'] = None,
) -> str:
  """Send a typed message or reply on a mission and return its resolved id."""
  checked_payload = chat_payload(payload)
  with connection(client) as connected:
    resolved, _ = _send(connected, mission_id, checked_payload, reply_to=reply_to, question=False)
  return resolved


@dataclass(frozen=True)
class Asked:
  mission_id: str
  question_id: str
  answer: Optional[dict[str, Any]] = None

  @property
  def state(self) -> str:
    return 'asked' if self.answer is None else 'answered'


def _entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
  payload = entry.get('head')
  if not isinstance(payload, dict):
    raise MissionError('mission conversation carried a malformed payload')
  return payload


def _reply_from_tail(mission: dict[str, Any], question_id: str) -> Optional[dict[str, Any]]:
  for entry in reversed(_entries(mission)):
    transition = entry.get('transition')
    if transition not in ('message', 'refused'):
      raise MissionError(f'mission conversation carried an unknown transition: {transition!r}')
    if transition == 'refused' and entry.get('id') == question_id:
      reason = entry.get('reason')
      if not isinstance(reason, str):
        raise MissionError('a refused mission chat entry carried no reason')
      raise MissionError(reason)
    if transition == 'message' and entry.get('reply_to') == question_id:
      return _entry_payload(entry)
  return None


def ask(
  mission_id: str,
  payload: dict[str, Any],
  *,
  reply_to: Optional[str] = None,
  wait: Optional[float] = None,
  client: Optional['Client'] = None,
) -> Asked:
  """Ask a typed question and optionally wait for its typed reply."""
  if wait is not None and (wait <= 0 or math.isnan(wait)):
    raise ValueError('wait must be a positive number of seconds')
  checked_payload = chat_payload(payload)
  with connection(client) as connected:
    resolved, sent = _send(connected, mission_id, checked_payload, reply_to=reply_to, question=True)
    question_id = sent.id
    assert question_id is not None
    if wait is None:
      return Asked(resolved, question_id)
    deadline = None if math.isinf(wait) else time.monotonic() + wait
    initial_remaining = _remaining(deadline)
    if initial_remaining is not None and initial_remaining <= 0:
      return Asked(resolved, question_id)
    try:
      current = query_mission(connected, resolved, read_timeout=initial_remaining)
    except _BrokerReadTimeout:
      if deadline is None or time.monotonic() < deadline:
        raise
      return Asked(resolved, question_id)

    def replied(mission: dict[str, Any]) -> bool:
      if _reply_from_tail(mission, question_id) is not None:
        return True
      _require_live(mission)
      return False

    current = _poll_mission(
      connected, resolved, current, deadline=deadline, done=replied, on_chat=True
    )
  return Asked(resolved, question_id, _reply_from_tail(current, question_id))


def asked_view(asked: Asked) -> dict[str, Any]:
  view: dict[str, Any] = {
    'state': asked.state,
    'mission_id': asked.mission_id,
    'question_id': asked.question_id,
  }
  if asked.answer is not None:
    view['answer'] = asked.answer
  return view


# --- cancel ---------------------------------------------------------------------


@dataclass(frozen=True)
class CancelStatus:
  state: str
  mission_id: str
  outcome: Optional[str] = None
  reason: Optional[str] = None
  trail_id: Optional[str] = None


def request_cancel(
  mission_id: str,
  *,
  client: Optional['Client'] = None,
  validate: Optional[Callable[[dict[str, Any]], None]] = None,
) -> CancelStatus:
  """Ask the host to cancel an owned mission and return once it accepts."""
  from bro.broker.dispatcher import CANCEL

  resolved = resolve(mission_id)
  with connection(client) as connected:
    if validate is not None:
      mission = query_mission(connected, resolved)
      caller_end(mission, resolved)
      validate(mission)
      _require_live(mission)
    call_ok(connected, CANCEL, {'id': resolved}, timeout=ACCEPT_TIMEOUT)
  return CancelStatus('accepted', resolved)


def cancel(
  mission_id: str,
  *,
  timeout: Optional[float] = None,
  client: Optional['Client'] = None,
  validate: Optional[Callable[[dict[str, Any]], None]] = None,
) -> CancelStatus:
  """End an owned mission and wait for it to end."""
  deadline = wait_deadline(True, timeout)
  resolved = resolve(mission_id)
  with connection(client) as connected:
    request_cancel(resolved, client=connected, validate=validate)
    mission = query_mission(connected, resolved, read_timeout=_remaining(deadline))
    mission = _poll_mission(
      connected, resolved, mission, deadline=deadline, done=_ended, on_chat=False
    )
  trail_id = mission.get('trail_id')
  trail_id = trail_id if isinstance(trail_id, str) else None
  if not _ended(mission):
    return CancelStatus('pending', resolved, trail_id=trail_id)
  if mission.get('state') == 'evicted':
    raise MissionError(
      f'mission {resolved!r} ended but its outcome is no longer retained; {trails_hint(trail_id)}'
    )
  outcome = mission.get('outcome')
  reason = mission.get('reason')
  if not isinstance(outcome, str) or (reason is not None and not isinstance(reason, str)):
    raise MissionError(f'mission {resolved!r} ended with a malformed outcome: {outcome!r}')
  return CancelStatus('ended', resolved, outcome=outcome, reason=reason, trail_id=trail_id)


def cancel_view(status: CancelStatus) -> dict[str, Any]:
  view: dict[str, Any] = {'state': status.state, 'mission_id': status.mission_id}
  for name in ('outcome', 'reason', 'trail_id'):
    value = getattr(status, name)
    if value is not None:
      view[name] = value
  return view


# --- list -----------------------------------------------------------------------


def _query_missions(client: 'Client') -> list[dict[str, Any]]:
  from bro.broker.dispatcher import QUERY

  missions: list[dict[str, Any]] = []
  cursor: Optional[str] = None
  while True:
    args = {} if cursor is None else {'cursor': cursor}
    value = _read_value(client, QUERY, args, timeout=ACCEPT_TIMEOUT)
    page = value.get('missions')
    if not isinstance(page, list) or not all(isinstance(mission, dict) for mission in page):
      raise MissionError('query listing returned malformed mission records')
    missions.extend(page)
    cursor = value.get('cursor')
    if cursor is None:
      return missions
    if not isinstance(cursor, str):
      raise MissionError('query listing returned a malformed cursor')


def _query_listing(client: 'Client', worker_type: Optional[str] = None) -> list[dict[str, Any]]:
  return [
    mission
    for mission in _query_missions(client)
    if mission.get('kind') == LAUNCH and (worker_type is None or mission.get('type') == worker_type)
  ]


def list_missions(worker_type: Optional[str] = None) -> dict[str, Any]:
  """Return caller-visible retained worker missions, optionally filtered by type."""
  if worker_type == '':
    raise ValueError('mission type must be non-empty')
  with open_client() as client:
    return {'missions': _query_listing(client, worker_type)}


@dataclass(frozen=True)
class LiveMission:
  mission_id: str
  type: str
  label: str


def live_mission_line(mission: LiveMission) -> str:
  if mission.type == BRO:
    return f'quest {mission.mission_id} to {mission.label}'
  return f'mission {mission.mission_id}: {mission.label}'


def live_missions() -> list[LiveMission]:
  """Return the live missions this session owns, newest first."""
  own = own_mission()
  with open_client() as client:
    records = _query_missions(client)
  missions: list[LiveMission] = []
  for record in records:
    if record.get('kind') != LAUNCH or record.get('parent') != own or _ended(record):
      continue
    worker_type = record.get('type')
    if not isinstance(worker_type, str):
      raise MissionError('query listing returned a live mission without a worker type')
    label = worker_type
    if worker_type == BRO:
      args = record.get('args')
      label = args.get('target') if isinstance(args, dict) else None
      if not isinstance(label, str):
        raise MissionError('query listing returned a live bro mission without a target')
    missions.append(LiveMission(_mission_id(record), worker_type, label))
  return missions


# --- watch ----------------------------------------------------------------------


def _single_line(text: str) -> str:
  return ''.join(
    character if character.isprintable() else repr(character)[1:-1] for character in text
  )


def _worker_type(event: dict[str, Any]) -> str:
  worker_type = event.get('type')
  if isinstance(worker_type, str):
    return worker_type
  if event.get('transition') == 'denied':
    args = event.get('args')
    requested = args.get('type') if isinstance(args, dict) else None
    return requested if isinstance(requested, str) else 'unknown'
  raise MissionError('mission event carried no worker type')


def _mission_clause(event: dict[str, Any], own: str) -> str:
  mission_id = event.get('mission')
  parent = event.get('parent')
  if not isinstance(mission_id, str) or parent != own:
    raise MissionError('events read returned a mission this session did not launch')
  worker_type = _worker_type(event)
  clause = f'mission {mission_id} ({worker_type}'
  args = event.get('args')
  target = args.get('target') if isinstance(args, dict) else None
  if worker_type == BRO and isinstance(target, str):
    clause += f' to {target}'
  return clause + ')'


def _payload_text(payload: Any) -> str:
  if isinstance(payload, dict) and set(payload) == {'text'} and isinstance(payload['text'], str):
    return payload['text']
  return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def _bounded_chat_line(line: str, event: dict[str, Any]) -> str:
  encoded = line.encode()
  if len(encoded) <= WATCH_LINE_BYTES:
    return line
  sequence = event.get('seq')
  mission_id = event.get('mission')
  if not isinstance(sequence, int) or isinstance(sequence, bool) or not isinstance(mission_id, str):
    raise MissionError('chat event carried no sequence or mission id')
  marker = f' [{len(encoded)} bytes, cut; seq {sequence} in mission history {mission_id}]'
  marker_bytes = marker.encode()
  if len(marker_bytes) >= WATCH_LINE_BYTES:
    raise MissionError('mission watch cut marker exceeds its line bound')
  prefix = encoded[: WATCH_LINE_BYTES - len(marker_bytes)]
  while True:
    try:
      cut = prefix.decode()
      break
    except UnicodeDecodeError as error:
      prefix = prefix[: error.start]
  return cut + marker


def _chat_event_line(event: dict[str, Any], own: str) -> Optional[str]:
  transition = event.get('transition')
  if transition not in ('message', 'refused'):
    return None
  if transition == 'message' and event.get('from') != 'worker':
    return None
  clause = _mission_clause(event, own)
  if transition == 'refused':
    reason = event.get('reason')
    if not isinstance(reason, str):
      raise MissionError('refused mission event carried no reason')
    head = event.get('head')
    rendered = f'{clause} refused {reason}'
    if not (isinstance(head, dict) and head.get('truncated') is True):
      rendered += f' {_payload_text(head)}'
  elif event.get('id') is not None:
    rendered = f'{clause} asks {_payload_text(event.get("head"))}'
  elif event.get('reply_to') is not None:
    rendered = f'{clause} replies {_payload_text(event.get("head"))}'
  else:
    rendered = f'{clause} says {_payload_text(event.get("head"))}'
  return _bounded_chat_line(_single_line(rendered), event)


def _event_line(event: dict[str, Any], own: str) -> str:
  transition = event.get('transition')
  if not isinstance(transition, str):
    raise MissionError('mission event carried no transition')
  if transition == 'trail':
    transition = f'trail {event.get("trail_id")}'
  elif transition == 'ended':
    reason = f':{event["reason"]}' if event.get('reason') is not None else ''
    transition = f'ended {event.get("outcome")}{reason}'
  elif transition == 'denied':
    transition = f'denied {event.get("reason")}'
  return _single_line(f'{_mission_clause(event, own)} {transition}')


def _chat_entry_event(mission: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
  mission_id = mission.get('id')
  parent = mission.get('parent')
  args = mission.get('args')
  if not isinstance(mission_id, str) or not isinstance(parent, str) or not isinstance(args, dict):
    raise MissionError('query returned a malformed mission for watch replay')
  return {
    'kind': mission.get('kind'),
    'type': mission.get('type'),
    'mission': mission_id,
    'parent': parent,
    'args': args,
    **entry,
  }


def _arm_replay(client: 'Client', own: str, head: int, worker_type: Optional[str]) -> list[str]:
  events: list[dict[str, Any]] = []
  for mission in _query_listing(client, worker_type):
    if _ended(mission):
      continue
    pending = mission.get('pending')
    if not isinstance(pending, list) or not all(isinstance(entry, dict) for entry in pending):
      raise MissionError('mission listing returned malformed pending questions')
    events.extend(_chat_entry_event(mission, entry) for entry in pending)
  replay: dict[int, str] = {}
  for event in events:
    sequence = event.get('seq')
    if not isinstance(sequence, int) or isinstance(sequence, bool):
      raise MissionError('watch replay entry carried a malformed sequence')
    if sequence > head:
      continue
    line = _chat_event_line(event, own)
    if line is None:
      continue
    marked = _single_line(f'before the watch: {line}')
    previous = replay.setdefault(sequence, marked)
    if previous != marked:
      raise MissionError(f'watch replay carried conflicting entries at sequence {sequence}')
  return [replay[sequence] for sequence in sorted(replay)]


def watch(
  worker_type: Optional[str] = None, wait_seconds: float = READ_WAIT_SECONDS
) -> Generator[str]:
  """Yield retained pending chat, then ordered lifecycle and chat events."""
  if worker_type == '':
    raise ValueError('mission type must be non-empty')
  if wait_seconds <= 0:
    raise MissionError('events wait must be positive')
  from bro.broker.dispatcher import EVENTS

  own = own_mission()
  with open_client() as client:
    baseline = _read_value(client, EVENTS, {}, timeout=ACCEPT_TIMEOUT)
    head = baseline.get('head')
    if not isinstance(head, int) or isinstance(head, bool):
      raise MissionError('events arm returned a malformed head')
    cursor = head
    yield from _arm_replay(client, own, head, worker_type)
    while True:
      try:
        value = _read_value(
          client,
          EVENTS,
          {'after': cursor, 'wait': wait_seconds},
          timeout=max(ACCEPT_TIMEOUT, wait_seconds + ACCEPT_TIMEOUT),
        )
      except MissionError as error:
        if not str(error).startswith('events gap:'):
          raise
        baseline = _read_value(client, EVENTS, {}, timeout=ACCEPT_TIMEOUT)
        head = baseline.get('head')
        if not isinstance(head, int) or isinstance(head, bool):
          raise MissionError('events re-arm returned a malformed head') from error
        cursor = head
        yield _single_line(f'mission watch gap: {error}; re-armed at {head}')
        yield from _arm_replay(client, own, head, worker_type)
        continue
      events = value.get('events')
      if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise MissionError('events read returned malformed records')
      for event in events:
        sequence = event.get('seq')
        if not isinstance(sequence, int) or isinstance(sequence, bool):
          raise MissionError('events read returned a malformed sequence')
        cursor = max(cursor, sequence)
        if event.get('kind') != LAUNCH or event.get('parent') != own:
          continue
        if worker_type is not None and _worker_type(event) != worker_type:
          continue
        chat_line = _chat_event_line(event, own)
        if chat_line is not None:
          yield chat_line
        elif event.get('transition') not in ('message', 'refused'):
          yield _event_line(event, own)


# --- CLI ------------------------------------------------------------------------


def _print_json(value: Any) -> None:
  print(json.dumps(value, indent=2, ensure_ascii=False))


def _check(mission_id: str) -> int:
  try:
    outcome = check(mission_id)
  except (MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  _print_json(outcome_view(outcome))
  return RUNNING_EXIT_CODE if outcome.state == 'running' else 0


def _history(mission_id: str, sequence: Optional[int]) -> int:
  try:
    conversation = history(mission_id, sequence=sequence)
  except (MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  if sequence is not None:
    _print_json(conversation.messages[0])
  else:
    _print_json(history_view(conversation))
  return 0


def _say(mission_id: str, payload: dict[str, Any], reply_to: Optional[str]) -> int:
  try:
    say(mission_id, payload, reply_to=reply_to)
  except (MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  return 0


def _ask(
  mission_id: str,
  payload: dict[str, Any],
  reply_to: Optional[str],
  wait: Optional[float],
) -> int:
  try:
    asked = ask(mission_id, payload, reply_to=reply_to, wait=wait)
  except (MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  if asked.state == 'answered':
    _print_json(asked.answer)
    return 0
  print(asked.question_id)
  return 0 if wait is None else QUESTION_EXIT_CODE


def _cancel(mission_id: str, timeout: Optional[float]) -> int:
  try:
    status = cancel(mission_id, timeout=timeout)
  except (MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  if status.state == 'pending':
    assert timeout is not None
    log.info('cancel accepted; mission %s has not ended within %.0fs', status.mission_id, timeout)
    return RUNNING_EXIT_CODE
  outcome = status.outcome if status.reason is None else f'{status.outcome}:{status.reason}'
  log.info('mission %s ended %s', status.mission_id, outcome)
  return 0


def _list(worker_type: Optional[str]) -> int:
  try:
    listing = list_missions(worker_type)
  except (MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  _print_json(listing)
  return 0


def _watch(worker_type: Optional[str]) -> int:
  try:
    for line in watch(worker_type):
      print(line, flush=True)
  except (MissionError, ValueError) as error:
    log.error('%s', error)
    return 1
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog='mission',
    description='read, talk to, watch, list, and end missions owned by this session',
  )
  verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

  check_parser = verbs.add_parser(
    'check',
    help="an owned mission's retained result, or that it still runs",
  )
  check_parser.add_argument('mission_id', metavar='<mission-id>', help='owned mission id')
  check_parser.set_handler(_check)

  history_parser = verbs.add_parser(
    'history',
    help="a mission's conversation, or one full entry by sequence",
  )
  history_parser.add_argument('mission_id', metavar='<mission-id>', help=MISSION_ID_HELP)
  history_parser.add_argument(
    '--seq', type=int, metavar='N', dest='sequence', help='print the retained chat entry at N'
  )
  history_parser.set_handler(_history)

  say_parser = verbs.add_parser('say', help='send a typed message or reply')
  say_parser.add_argument('mission_id', metavar='<mission-id>', help=MISSION_ID_HELP)
  say_parser.add_argument('payload', type=payload_argument, help='JSON object to send')
  say_parser.add_argument('--reply-to', metavar='<question-id>', help='question this answers')
  say_parser.set_handler(_say)

  ask_parser = verbs.add_parser('ask', help='send a typed question; --wait blocks for its reply')
  ask_parser.add_argument('mission_id', metavar='<mission-id>', help=MISSION_ID_HELP)
  ask_parser.add_argument('payload', type=payload_argument, help='JSON object to send')
  ask_parser.add_argument(
    '--reply-to', metavar='<question-id>', help='question this counter-question answers'
  )
  ask_parser.add_argument(
    '--wait',
    nargs='?',
    const=math.inf,
    type=float,
    metavar='SECONDS',
    help='block for the reply, without a value until it arrives or the mission ends',
  )
  ask_parser.set_handler(_ask)

  list_parser = verbs.add_parser('list', help='retained owned missions, live first')
  list_parser.add_argument('--type', dest='worker_type', metavar='TYPE', help='worker type to keep')
  list_parser.set_handler(_list)

  watch_parser = verbs.add_parser('watch', help='stream owned mission lifecycle and chat')
  watch_parser.add_argument(
    '--type', dest='worker_type', metavar='TYPE', help='worker type to keep'
  )
  watch_parser.set_handler(_watch)

  cancel_parser = verbs.add_parser('cancel', help='end an owned mission')
  cancel_parser.add_argument('mission_id', metavar='<mission-id>', help='owned mission id')
  cancel_parser.add_argument(
    '--timeout', type=float, metavar='SECONDS', help='maximum seconds to wait for the end'
  )
  cancel_parser.set_handler(_cancel)

  return parser.dispatch(argv)
