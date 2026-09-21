"""quest — read, talk to, and end a quest through the session broker.

A quest is what a summon opens: another session working a request until it
answers. This module owns the outcome read, conversation read, say, ask, and
caller-scoped listing, ordered watch, and cancellation for bro quests,
as a library and as the ``quest`` CLI. ``self`` names the
session's own quest, the one it answers.

Reads are repeatable journal queries. A wait bounds silence rather than the
quest: it re-reads the journal through bounded long-polls until the state it
waits for or its deadline, and a caller past the deadline sees the state it
stopped at. Watch replays retained chat through the head it arms at, then
long-polls the ordered event stream after it.

Unlike the substrate CLI, an unset ``BROKER_CHANNEL`` is an error.
Broker imports stay deferred so importing the constants does not pull in the
broker implementation on pre-gate launch paths.
"""

import json
import math
import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import bro.base.args as base_args
from bro import mission as mission_client
from bro.base import log

if TYPE_CHECKING:
  from bro.broker.brotocol import End, Message
  from bro.broker.client import Client

__cli_name__ = 'quest'

LAUNCH = mission_client.LAUNCH
BRO = mission_client.BRO
SELF = mission_client.SELF
ACCEPT_TIMEOUT = mission_client.ACCEPT_TIMEOUT
READ_WAIT_SECONDS = mission_client.READ_WAIT_SECONDS
RUNNING_EXIT_CODE = mission_client.RUNNING_EXIT_CODE
QUESTION_EXIT_CODE = mission_client.QUESTION_EXIT_CODE
QUEST_ID_HELP = f"quest id; `{SELF}` names this session's own quest"


QuestError = mission_client.MissionError
_BrokerReadTimeout = mission_client._BrokerReadTimeout
open_client = mission_client.open_client
connection = mission_client.connection
own_quest = mission_client.own_mission
resolve = mission_client.resolve
caller_end = mission_client.caller_end
trails_hint = mission_client.trails_hint
call_ok = mission_client.call_ok
_read_value = mission_client._read_value
query_quest = mission_client.query_mission
_poll_quest = mission_client._poll_mission
wait_deadline = mission_client.wait_deadline
_remaining = mission_client._remaining
_quest_id = mission_client._mission_id
_chat_seq = mission_client._chat_seq
_ended = mission_client._ended


def interpret_result(payload: dict[str, Any], trail_id: Optional[str]) -> str:
  """Turn a summon result payload into its answer or an operator-facing error."""
  outcome = payload.get('outcome')
  if outcome == 'ok':
    value = payload.get('value')
    return value if value is not None else ''
  if outcome == 'denied':
    raise QuestError(str(payload.get('error', payload)))
  detail = payload.get('detail')
  detail = detail if isinstance(detail, dict) else {}
  reason = detail.get('reason')
  error = payload.get('error')
  if reason in ('raised', 'error'):
    raise QuestError(f'summon {reason}: {error}')
  parts = [f'summon failed ({reason})']
  diagnostic = error if error is not None else detail.get('output_tail')
  if diagnostic is not None and len(str(diagnostic).strip()) > 0:
    parts.append(str(diagnostic).strip())
  parts.append(trails_hint(trail_id))
  raise QuestError('; '.join(parts))


def _require_bro(quest: dict[str, Any]) -> None:
  quest_id = quest.get('id')
  if quest.get('state') == 'evicted':
    raise QuestError(f'quest {quest_id!r} is no longer retained')
  if quest.get('kind') != LAUNCH or quest.get('type') != BRO:
    raise QuestError(f'quest {quest_id!r} is not a bro launch')


def answer_of(quest: dict[str, Any]) -> Optional[str]:
  """the retained answer of a summon quest, None while it runs; a failed,
  denied, or evicted quest raises with the reason."""
  quest_id = quest.get('id')
  state = quest.get('state')
  if state != 'evicted':
    _require_bro(quest)
  if state in ('accepted', 'started'):
    return None
  trail_id = quest.get('trail_id')
  trail_id = trail_id if isinstance(trail_id, str) else None
  if state == 'evicted' or quest.get('result_evicted') is True:
    raise QuestError(f'the quest result is no longer retained; {trails_hint(trail_id)}')
  if state not in ('ended', 'denied'):
    raise QuestError(f'quest {quest_id!r} has unknown state {state!r}')
  result = quest.get('result')
  if not isinstance(result, dict):
    raise QuestError(f'quest {quest_id!r} has no retained result; {trails_hint(trail_id)}')
  return interpret_result(result, trail_id)


def _require_live(quest: dict[str, Any]) -> None:
  if answer_of(quest) is not None:
    raise QuestError(f'quest {quest.get("id")!r} already ended successfully')


def _text_from_payload(payload: Any) -> str:
  if (
    not isinstance(payload, dict)
    or set(payload) != {'text'}
    or not isinstance(payload['text'], str)
  ):
    raise QuestError(f'quest chat carried a malformed text payload: {payload!r}')
  return payload['text']


def _text_from_entry(entry: dict[str, Any]) -> str:
  return _text_from_payload(entry.get('head'))


def _refused_text(entry: dict[str, Any]) -> Optional[str]:
  head = entry.get('head')
  if isinstance(head, dict) and head.get('truncated') is True:
    return None
  return _text_from_payload(head)


def chat_payload(text: str) -> dict[str, Any]:
  """The chat payload carrying `text`, refused over the message bound."""
  from bro.broker.journal import oversized_message

  if not isinstance(text, str) or not text:
    raise ValueError('quest chat text must be a non-empty string')
  payload = {'text': text}
  reason = oversized_message(payload)
  if reason is not None:
    raise QuestError(f'{reason}; mint an artifact and send the ref')
  return payload


_talk_of = mission_client._talk_of
_entries = mission_client._entries


@dataclass(frozen=True)
class Question:
  id: str
  text: str
  quest_id: str


def question_from_message(message: 'Message') -> Question:
  if message.id is None:
    raise QuestError('quest question carried no id')
  return Question(message.id, _text_from_payload(message.payload), message.request_id)


def open_questions(quest: dict[str, Any], *, awaiting: 'End') -> tuple[Question, ...]:
  """the open questions the `awaiting` end owes a reply to: the other end's
  marked entries, oldest first."""
  other = 'worker' if awaiting == 'owner' else 'owner'
  questions = []
  for entry in _entries(quest):
    if entry.get('pending') is not True or entry.get('from') != other:
      continue
    question_id = entry.get('id')
    if not isinstance(question_id, str):
      raise QuestError('an open quest question carried no id')
    questions.append(Question(question_id, _text_from_entry(entry), _quest_id(quest)))
  return tuple(questions)


# --- check ----------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
  """what a quest this session summoned came to: its answer once it has one,
  otherwise that it runs — and, on a wait, the open questions the child is
  stalled on."""

  quest_id: str
  answer: Optional[str] = None
  trail_id: Optional[str] = None
  questions: tuple[Question, ...] = ()

  @property
  def state(self) -> str:
    if self.answer is not None:
      return 'completed'
    if len(self.questions) > 0:
      return 'question'
    return 'running'


def _outcome_of(quest: dict[str, Any], quest_id: str, caller: 'End') -> Outcome:
  answer = answer_of(quest)
  trail_id = quest.get('trail_id')
  questions: tuple[Question, ...] = ()
  if answer is None and caller == 'owner':
    questions = open_questions(quest, awaiting='owner')
  return Outcome(quest_id, answer, trail_id if isinstance(trail_id, str) else None, questions)


def check(
  quest_id: str,
  *,
  wait: bool = False,
  timeout: Optional[float] = None,
  client: Optional['Client'] = None,
) -> Outcome:
  """Read the outcome of a quest this session summoned, without consuming it.

  A running quest reports as such and a completed one carries its answer, while a
  failed, denied, or evicted one raises with the reason. A direct child stalled on
  open questions for this session reports them in place of running, since they
  wait on its reply. `wait` blocks until the quest ends or such a question opens,
  or at `timeout` with the state it stopped at."""
  deadline = wait_deadline(wait, timeout)
  resolved = resolve(quest_id)
  if resolved == own_quest():
    raise QuestError('a session cannot check the quest it answers; the outcome is its own to give')
  with connection(client) as connected:
    quest = query_quest(connected, resolved, read_timeout=_remaining(deadline))
    caller = caller_end(quest, resolved)
    if wait:
      quest = _poll_quest(
        connected,
        resolved,
        quest,
        deadline=deadline,
        done=lambda current: _outcome_of(current, resolved, caller).state != 'running',
        on_chat=caller == 'owner',
      )
  return _outcome_of(quest, resolved, caller)


def outcome_view(outcome: Outcome) -> dict[str, Any]:
  view: dict[str, Any] = {'state': outcome.state, 'quest_id': outcome.quest_id}
  if outcome.trail_id is not None:
    view['trail_id'] = outcome.trail_id
  if outcome.state == 'completed':
    view['answer'] = outcome.answer
  if outcome.state == 'question':
    view['questions'] = [
      {'id': question.id, 'text': question.text} for question in outcome.questions
    ]
  return view


# --- history --------------------------------------------------------------------


@dataclass(frozen=True)
class History:
  """a quest's conversation as the journal retains it: the talk rights, the
  message tail with every open question marked in place, and whether older
  entries were dropped."""

  quest_id: str
  talk: tuple[str, ...]
  messages: tuple[dict[str, Any], ...]
  truncated: bool = False
  chat_seq: int = 0
  awaiting: tuple[Question, ...] = ()  # the open questions the caller owes a reply to
  ended: bool = False


def _history_of(quest: dict[str, Any], quest_id: str, caller: 'End') -> History:
  if quest.get('state') == 'evicted':
    raise QuestError(f'quest {quest_id!r} is no longer retained')
  truncated = quest.get('messages_truncated', False)
  if not isinstance(truncated, bool):
    raise QuestError('quest query returned a malformed conversation truncation marker')
  return History(
    quest_id,
    tuple(sorted(_talk_of(quest))),
    tuple(_entries(quest)),
    truncated,
    _chat_seq(quest),
    open_questions(quest, awaiting=caller),
    _ended(quest),
  )


def history(
  quest_id: str,
  *,
  wait: bool = False,
  timeout: Optional[float] = None,
  client: Optional['Client'] = None,
) -> History:
  """Read a quest's conversation without consuming it.

  `wait` blocks until the next message or the end, returning at once while a
  question awaits the caller, or at `timeout` with the state it stopped at."""
  deadline = wait_deadline(wait, timeout)
  resolved = resolve(quest_id)
  with connection(client) as connected:
    quest = query_quest(connected, resolved, read_timeout=_remaining(deadline))
    caller = caller_end(quest, resolved)
    _require_bro(quest)
    if wait:
      cursor = _history_of(quest, resolved, caller).chat_seq

      def done(current: dict[str, Any]) -> bool:
        latest = _history_of(current, resolved, caller)
        return latest.ended or len(latest.awaiting) > 0 or latest.chat_seq > cursor

      quest = _poll_quest(connected, resolved, quest, deadline=deadline, done=done, on_chat=True)
  return _history_of(quest, resolved, caller)


def history_view(history: History) -> dict[str, Any]:
  view: dict[str, Any] = {
    'quest_id': history.quest_id,
    'talk': list(history.talk),
    'messages': list(history.messages),
  }
  if history.truncated:
    view['truncated'] = True
  return view


# --- say and ask ----------------------------------------------------------------


def _send(
  client: 'Client',
  quest_id: str,
  payload: dict[str, Any],
  *,
  reply_to: Optional[str],
  question: bool,
) -> tuple[str, 'Message']:
  return mission_client._send(
    client,
    quest_id,
    payload,
    reply_to=reply_to,
    question=question,
    validate=_require_bro,
  )


def say(
  quest_id: str,
  text: str,
  *,
  reply_to: Optional[str] = None,
  client: Optional['Client'] = None,
) -> str:
  """Send a message that expects no reply — or, with `reply_to`, a reply — on a
  quest; returns the id it was sent under."""
  payload = chat_payload(text)
  with connection(client) as connected:
    resolved, _ = _send(connected, quest_id, payload, reply_to=reply_to, question=False)
  return resolved


@dataclass(frozen=True)
class Asked:
  quest_id: str
  question_id: str
  answer: Optional[str] = None

  @property
  def state(self) -> str:
    return 'asked' if self.answer is None else 'answered'


def _reply_from_tail(quest: dict[str, Any], question_id: str) -> Optional[str]:
  for entry in reversed(_entries(quest)):
    transition = entry.get('transition')
    if transition not in ('message', 'refused'):
      raise QuestError(f'quest conversation carried an unknown transition: {transition!r}')
    if transition == 'refused' and entry.get('id') == question_id:
      reason = entry.get('reason')
      if not isinstance(reason, str):
        raise QuestError('a refused quest chat entry carried no reason')
      raise QuestError(reason)
    if transition == 'message' and entry.get('reply_to') == question_id:
      return _text_from_entry(entry)
  return None


def ask(
  quest_id: str,
  text: str,
  *,
  reply_to: Optional[str] = None,
  wait: Optional[float] = None,
  client: Optional['Client'] = None,
) -> Asked:
  """Ask a question on a quest — a counter-question with `reply_to` — and return
  its id. `wait` (seconds, infinite allowed) blocks for the reply, returning the
  id alone at the bound; the host keeps the question live either way."""
  if wait is not None and (wait <= 0 or math.isnan(wait)):
    raise ValueError('wait must be a positive number of seconds')
  payload = chat_payload(text)
  with connection(client) as connected:
    resolved, sent = _send(connected, quest_id, payload, reply_to=reply_to, question=True)
    question_id = sent.id
    assert question_id is not None
    if wait is None:
      return Asked(resolved, question_id)
    deadline = None if math.isinf(wait) else time.monotonic() + wait
    initial_remaining = _remaining(deadline)
    if initial_remaining is not None and initial_remaining <= 0:
      return Asked(resolved, question_id)
    try:
      current = query_quest(connected, resolved, read_timeout=initial_remaining)
    except _BrokerReadTimeout:
      if deadline is None or time.monotonic() < deadline:
        raise
      return Asked(resolved, question_id)

    def replied(quest: dict[str, Any]) -> bool:
      if _reply_from_tail(quest, question_id) is not None:
        return True
      _require_live(quest)
      return False

    current = _poll_quest(
      connected, resolved, current, deadline=deadline, done=replied, on_chat=True
    )
  return Asked(resolved, question_id, _reply_from_tail(current, question_id))


def asked_view(asked: Asked) -> dict[str, Any]:
  view: dict[str, Any] = {
    'state': asked.state,
    'quest_id': asked.quest_id,
    'question_id': asked.question_id,
  }
  if asked.answer is not None:
    view['answer'] = asked.answer
  return view


# --- cancel ---------------------------------------------------------------------


@dataclass(frozen=True)
class CancelStatus:
  state: str
  quest_id: str
  outcome: Optional[str] = None
  reason: Optional[str] = None
  trail_id: Optional[str] = None


def _quest_cancel_status(status: mission_client.CancelStatus) -> CancelStatus:
  return CancelStatus(
    status.state,
    status.mission_id,
    outcome=status.outcome,
    reason=status.reason,
    trail_id=status.trail_id,
  )


def request_cancel(quest_id: str, *, client: Optional['Client'] = None) -> CancelStatus:
  """Ask the host to cancel an owned bro quest and return once it accepts."""
  status = mission_client.request_cancel(quest_id, client=client, validate=_require_bro)
  return _quest_cancel_status(status)


def cancel(
  quest_id: str,
  *,
  timeout: Optional[float] = None,
  client: Optional['Client'] = None,
) -> CancelStatus:
  """End an owned bro quest and wait for it to end."""
  status = mission_client.cancel(
    quest_id,
    timeout=timeout,
    client=client,
    validate=_require_bro,
  )
  return _quest_cancel_status(status)


def cancel_view(status: CancelStatus) -> dict[str, Any]:
  view: dict[str, Any] = {'state': status.state, 'quest_id': status.quest_id}
  for name in ('outcome', 'reason', 'trail_id'):
    value = getattr(status, name)
    if value is not None:
      view[name] = value
  return view


# --- list -----------------------------------------------------------------------


_query_missions = mission_client._query_missions
LiveMission = mission_client.LiveMission
live_mission_line = mission_client.live_mission_line
live_missions = mission_client.live_missions


def _query_listing(client: 'Client') -> list[dict[str, Any]]:
  return [
    mission
    for mission in _query_missions(client)
    if mission.get('kind') == LAUNCH and mission.get('type') == BRO
  ]


def list_quests() -> dict[str, Any]:
  """Return every caller-visible retained summon record, live first."""
  with open_client() as client:
    return {'quests': _query_listing(client)}


# --- watch ----------------------------------------------------------------------


def _single_line(text: str) -> str:
  return ''.join(
    character if character.isprintable() else repr(character)[1:-1] for character in text
  )


def _quest_clause(event: dict[str, Any], own: str) -> str:
  args = event.get('args')
  parent = event.get('parent')
  if not isinstance(args, dict) or not isinstance(parent, str):
    raise QuestError('events read returned a malformed summon record')
  clause = f'quest {event.get("mission")}'
  target = args.get('target')
  if isinstance(target, str):
    clause += f' to {target}'
  elif event.get('transition') != 'denied':
    raise QuestError('events read returned an accepted summon without a target')
  if parent != own:
    raise QuestError('events read returned a quest this session did not summon')
  return clause


def _chat_event_line(event: dict[str, Any], own: str) -> Optional[str]:
  transition = event.get('transition')
  is_own_quest = event.get('mission') == own
  sender = event.get('from')
  other_end = sender == ('owner' if is_own_quest else 'worker')
  if transition == 'message' and not other_end:
    return None
  if transition == 'listening':
    if is_own_quest:
      return None
    return _single_line(f'summon listening ({_quest_clause(event, own)})')
  if transition not in ('message', 'refused'):
    return None
  actor = 'summoner' if is_own_quest else 'summon'
  if transition == 'refused':
    reason = event.get('reason')
    if not isinstance(reason, str):
      raise QuestError('refused summon event carried no reason')
    head = f'{actor} refused {reason}'
    text = _refused_text(event)
    if text is not None:
      head += f': {text}'
  elif event.get('id') is not None:
    head = f'{actor} asks {_text_from_entry(event)}'
  elif event.get('reply_to') is not None:
    head = f'{actor} replies {_text_from_entry(event)}'
  else:
    head = f'{actor} says {_text_from_entry(event)}'
  details = []
  if not is_own_quest:
    details.append(_quest_clause(event, own))
  if event.get('id') is not None:
    details.append(f'question {event["id"]}')
  if event.get('reply_to') is not None:
    details.append(f'to {event["reply_to"]}')
  return _single_line(head if not details else f'{head} ({", ".join(details)})')


def _event_line(event: dict[str, Any], own: str) -> str:
  transition = event.get('transition')
  if transition == 'trail':
    head = f'summon trail {event.get("trail_id")}'
  elif transition == 'ended':
    reason = f':{event["reason"]}' if event.get('reason') is not None else ''
    head = f'summon ended {event.get("outcome")}{reason}'
  elif transition == 'denied':
    head = str(event.get('reason'))
  else:
    head = f'summon {transition}'
  return _single_line(f'{head} ({_quest_clause(event, own)})')


def _chat_entry_event(quest: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
  quest_id = quest.get('id')
  parent = quest.get('parent')
  args = quest.get('args')
  if not isinstance(quest_id, str) or not isinstance(parent, str) or not isinstance(args, dict):
    raise QuestError('query returned a malformed quest for watch replay')
  return {
    'kind': quest.get('kind'),
    'type': quest.get('type'),
    'mission': quest_id,
    'parent': parent,
    'args': args,
    **entry,
  }


def _arm_replay(client: 'Client', own: str, head: int) -> list[str]:
  record = query_quest(client, own)
  events = [
    _chat_entry_event(record, entry) for entry in _entries(record) if entry.get('from') == 'owner'
  ]
  for quest in _query_listing(client):
    state = quest.get('state')
    if state in ('ended', 'denied', 'evicted'):
      continue
    if state not in ('accepted', 'started'):
      raise QuestError(f'quest listing returned an unknown state: {state!r}')
    pending = quest.get('pending')
    if not isinstance(pending, list) or not all(isinstance(entry, dict) for entry in pending):
      raise QuestError('quest listing returned malformed pending questions')
    events.extend(_chat_entry_event(quest, entry) for entry in pending)

  replay: dict[int, str] = {}
  for event in events:
    sequence = event.get('seq')
    if not isinstance(sequence, int) or isinstance(sequence, bool):
      raise QuestError('watch replay entry carried a malformed sequence')
    if sequence > head:
      continue
    line = _chat_event_line(event, own)
    if line is None:
      continue
    marked = _single_line(f'before the watch: {line}')
    previous = replay.setdefault(sequence, marked)
    if previous != marked:
      raise QuestError(f'watch replay carried conflicting entries at sequence {sequence}')
  return [replay[sequence] for sequence in sorted(replay)]


def watch(wait_seconds: float = READ_WAIT_SECONDS) -> Generator[str]:
  """Yield retained quest chat at arm, then ordered launch transitions."""
  if wait_seconds <= 0:
    raise QuestError('events wait must be positive')
  from bro.broker.dispatcher import EVENTS
  from bro.broker.environment import BROKER_MISSION

  with open_client() as client:
    own = os.environ.get(BROKER_MISSION)
    if own is None:
      raise QuestError(
        f'broker channel present but {BROKER_MISSION} unset; '
        'the launch did not name the quest this session answers'
      )
    baseline = _read_value(client, EVENTS, {}, timeout=ACCEPT_TIMEOUT)
    head = baseline.get('head')
    if not isinstance(head, int) or isinstance(head, bool):
      raise QuestError('events arm returned a malformed head')
    cursor = head
    yield from _arm_replay(client, own, head)
    while True:
      try:
        value = _read_value(
          client,
          EVENTS,
          {'after': cursor, 'wait': wait_seconds},
          timeout=max(ACCEPT_TIMEOUT, wait_seconds + ACCEPT_TIMEOUT),
        )
      except QuestError as error:
        if not str(error).startswith('events gap:'):
          raise
        baseline = _read_value(client, EVENTS, {}, timeout=ACCEPT_TIMEOUT)
        head = baseline.get('head')
        if not isinstance(head, int) or isinstance(head, bool):
          raise QuestError('events re-arm returned a malformed head') from error
        cursor = head
        yield _single_line(f'quest watch gap: {error}; re-armed at {head}')
        yield from _arm_replay(client, own, head)
        continue
      events = value.get('events')
      if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise QuestError('events read returned malformed records')
      for event in events:
        sequence = event.get('seq')
        if not isinstance(sequence, int) or isinstance(sequence, bool):
          raise QuestError('events read returned a malformed sequence')
        cursor = max(cursor, sequence)
        if event.get('kind') != LAUNCH:
          continue
        if event.get('type') != BRO:
          continue
        chat_line = _chat_event_line(event, own)
        if chat_line is not None:
          yield chat_line
        elif event.get('transition') not in ('message', 'refused', 'listening'):
          yield _event_line(event, own)


# --- CLI ------------------------------------------------------------------------


def _print_json(view: dict[str, Any]) -> None:
  print(json.dumps(view, indent=2, ensure_ascii=False))


def _check(quest_id: str, wait: bool, timeout: Optional[float]) -> int:
  try:
    outcome = check(quest_id, wait=wait, timeout=timeout)
  except (QuestError, ValueError) as error:
    log.error('%s', error)
    return 1
  if outcome.state == 'completed':
    print(outcome.answer)
    return 0
  _print_json(outcome_view(outcome))
  if outcome.state == 'question':
    return QUESTION_EXIT_CODE
  log.info('quest still running; %s', trails_hint(outcome.trail_id))
  return RUNNING_EXIT_CODE


def _history(quest_id: str, wait: bool, timeout: Optional[float]) -> int:
  try:
    conversation = history(quest_id, wait=wait, timeout=timeout)
  except (QuestError, ValueError) as error:
    log.error('%s', error)
    return 1
  _print_json(history_view(conversation))
  return QUESTION_EXIT_CODE if len(conversation.awaiting) > 0 else 0


def _say(quest_id: str, text: str, reply_to: Optional[str]) -> int:
  try:
    say(quest_id, text, reply_to=reply_to)
  except (QuestError, ValueError) as error:
    log.error('%s', error)
    return 1
  return 0


def _ask(quest_id: str, text: str, reply_to: Optional[str], wait: Optional[float]) -> int:
  try:
    asked = ask(quest_id, text, reply_to=reply_to, wait=wait)
  except (QuestError, ValueError) as error:
    log.error('%s', error)
    return 1
  if asked.state == 'answered':
    print(asked.answer)
    return 0
  print(asked.question_id)
  return 0 if wait is None else QUESTION_EXIT_CODE


def _cancel(quest_id: str, timeout: Optional[float]) -> int:
  try:
    status = cancel(quest_id, timeout=timeout)
  except (QuestError, ValueError) as error:
    log.error('%s', error)
    return 1
  if status.state == 'pending':
    assert timeout is not None
    log.info('cancel accepted; quest %s has not ended within %.0fs', status.quest_id, timeout)
    return RUNNING_EXIT_CODE
  outcome = status.outcome if status.reason is None else f'{status.outcome}:{status.reason}'
  log.info('quest %s ended %s', status.quest_id, outcome)
  return 0


def _list() -> int:
  try:
    listing = list_quests()
  except QuestError as error:
    log.error('%s', error)
    return 1
  _print_json(listing)
  return 0


def _watch() -> int:
  try:
    for line in watch():
      print(line, flush=True)
  except QuestError as error:
    log.error('%s', error)
    return 1
  return 0


def _add_wait(parser: base_args.Parser, *, until: str) -> None:
  parser.add_argument(
    '--wait',
    action='store_true',
    help=f'block until {until}, or --timeout passes; reads are non-destructive, so '
    'concurrent waits and later reads see the same state',
  )
  parser.add_argument(
    '--timeout',
    type=float,
    metavar='SECONDS',
    help='with --wait: maximum seconds to block before reporting the current state',
  )


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog='quest',
    description='read, talk to, and end the quests this session summoned, or talk on '
    f'its own quest as `{SELF}`; a summon opens one and prints its id',
  )
  verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

  check_parser = verbs.add_parser(
    'check',
    help='the outcome of a child quest: its answer, or that it still runs',
    description='read the outcome of a child quest through the host journal, without '
    f'consuming it: print the answer and exit 0 once it is in, exit {RUNNING_EXIT_CODE} '
    f'while the quest runs, exit {QUESTION_EXIT_CODE} with the open questions the child is '
    'stalled on, and exit 1 with the reason when it failed or was denied',
  )
  check_parser.add_argument(
    'quest_id',
    metavar='<quest-id>',
    help=f'child quest id; `{SELF}` is refused, since the outcome is this session’s to give',
  )
  _add_wait(check_parser, until='the quest ends or a child question awaits a reply')
  check_parser.set_handler(_check)

  history_parser = verbs.add_parser(
    'history',
    help="a quest's conversation: talk rights and the tail, open questions marked",
    description="read a quest's conversation through the host journal: its talk rights and "
    'the retained message tail with every open question marked `pending`, `truncated` '
    f'when older entries were dropped; exits {QUESTION_EXIT_CODE} when a question awaits '
    'this session, else 0',
  )
  history_parser.add_argument('quest_id', metavar='<quest-id>', help=QUEST_ID_HELP)
  _add_wait(history_parser, until='the next message or the end')
  history_parser.set_handler(_history)

  say_parser = verbs.add_parser(
    'say',
    help='send a message that expects no reply, or a reply',
    description='send a message on a quest: a say, or with --reply-to a reply to a question',
  )
  say_parser.add_argument('quest_id', metavar='<quest-id>', help=QUEST_ID_HELP)
  say_parser.add_argument(
    'text',
    help='message text; over the message bound it is refused, and an artifact ref goes instead',
  )
  say_parser.add_argument('--reply-to', metavar='<question-id>', help='question this answers')
  say_parser.set_handler(_say)

  ask_parser = verbs.add_parser(
    'ask',
    help='ask a question; --wait blocks for its reply',
    description='ask a question on a quest and print its id — with --reply-to a counter-question '
    'to the question named; --wait blocks for the reply and prints it, and at its bound '
    f'prints the question id and exits {QUESTION_EXIT_CODE}, the question still live',
  )
  ask_parser.add_argument('quest_id', metavar='<quest-id>', help=QUEST_ID_HELP)
  ask_parser.add_argument(
    'text',
    help='question text; over the message bound it is refused, and an artifact ref goes instead',
  )
  ask_parser.add_argument(
    '--reply-to', metavar='<question-id>', help='question this counter-question answers'
  )
  ask_parser.add_argument(
    '--wait',
    nargs='?',
    const=math.inf,
    type=float,
    metavar='SECONDS',
    help='block for the reply, without a value until it arrives or the quest ends',
  )
  ask_parser.set_handler(_ask)

  list_parser = verbs.add_parser(
    'list',
    help="this session's retained summon records, live first",
    description="list this session's retained summon journal records, live first; "
    'each id is a repeatable handle for `quest check` and `quest history`',
  )
  list_parser.set_handler(_list)

  watch_parser = verbs.add_parser(
    'watch',
    help='stream every bro quest this session summons',
    description='stream the ordered lifecycle and chat of every bro quest this session '
    'summons. Runs until killed; what is already in flight when it starts is the baseline',
  )
  watch_parser.set_handler(_watch)

  cancel_parser = verbs.add_parser(
    'cancel',
    help='end a bro quest this session owns',
    description='end a bro quest this session owns: it ends failed:cancelled and whatever it '
    'launched in turn ends failed:orphaned; a host-supervised worker is killed and an expected '
    f'worker detached; exits 0 once it ends and {RUNNING_EXIT_CODE} when --timeout passes first',
  )
  cancel_parser.add_argument('quest_id', metavar='<quest-id>', help='owned bro quest id')
  cancel_parser.add_argument(
    '--timeout',
    type=float,
    metavar='SECONDS',
    help='maximum seconds to wait for the quest to end; omitted waits until it has',
  )
  cancel_parser.set_handler(_cancel)

  return parser.dispatch(argv)
