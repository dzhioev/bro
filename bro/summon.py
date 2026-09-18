"""summon — request another bro through the session broker.

The module owns the summon request shape and every peer-side surface: blocking,
detached, manual, quest chat, query-backed check/list, and the journal event watch.
A detached or manual request returns its quest id only after the first correlated
message is the host's ``accepted`` mark; an immediate result is interpreted as
the refusal or launch failure it carries.

Blocking waits bound silence rather than the run.
After silence they query the journal by quest id, interpret a retained terminal
result, or resume waiting while the host-owned Worker deadline keeps the quest
live.
Check and list are repeatable journal reads, and watch long-polls the ordered
event stream from the head it arms at.

Unlike the substrate CLI, an unset ``BROKER_CHANNEL`` is an error.
Broker imports stay deferred so importing summon constants does not pull in the
broker implementation on pre-gate launch paths.
"""

import contextlib
import json
import math
import os
import shlex
import time
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, cast

import bro.base.args as base_args
from bro.base import log
from bro.base.scope import PARTY_PERMITS, permit_choices
from bro.launch.llm_flags import (
  EFFORT_HELP,
  FAST_HELP,
  add_llm_flags,
  canonicalize,
  selection_from_args,
)
from bro.llm.providers import LLMSelectionError
from bro.mcp import HOLDS

if TYPE_CHECKING:
  from bro.broker.brotocol import End, Message, Talk
  from bro.broker.client import Client

__cli_name__ = 'summon'

SUMMON = 'summon'  # the kind a summon request names
SUMMONER_ENV = 'RIDE_SUMMONER'
# marks a run as a summoned child, written by the surface that launches it
SUMMONED_ENV = 'RIDE_SUMMONED'
# carries a run's own effective summon allow-list into it, written by the surface
# that launches the run: a session root's at launch, a summoned child's at its spawn
MAY_SUMMON_ENV = 'RIDE_MAY_SUMMON'
PERMITS_ENV = 'RIDE_PERMITS'
PARTY_MEMBER_ENV = 'RIDE_PARTY_MEMBER'
RUNTIME_ENV = 'RIDE_RUNTIME'
# request-lifecycle bound for a summoned child — sized so the flagship deploy
# workload survives the default; the substrate's generic 600s default is untouched
DEFAULT_TIMEOUT = 1800.0
# acceptance and inline-read replies should arrive promptly; the bound turns a
# wedged broker into a clean client failure
ACCEPT_TIMEOUT = 30.0
# journal long-polls stay well inside harness-side MCP call budgets
READ_WAIT_SECONDS = 25.0
# `summon check` exit code while the result is not in yet (0 = answer relayed,
# 1 = failure, 2 = argparse usage error)
PENDING_EXIT_CODE = 3
QUESTION_EXIT_CODE = 4
HOLD_HELP = "the child's user-involvement level; omitted lets the child use its unattended default"
MANUAL_HELP = (
  'register a manual summon instead of spawning: the request id becomes the token '
  'a user-launched `ride along --summoned <token>` session answers; '
  '`ride solo --summoned <token>` runs the request one-shot'
)
HARNESS_HELP = (
  "the harness the child runs under: 'bro' (the target's own LLM process) or 'claude' "
  "(a one-shot managed Claude Code session); omitted runs it under the project's "
  '`[tool.bro] summon-harness`'
)
GRANT_HELP = (
  'add a credential (KIND or KIND+INSTANCE), summonable bro (@BRO), or party permit '
  f"({permit_choices()}) to the child's scope (repeatable)"
)
REVOKE_HELP = (
  "remove a credential kind (KIND), summonable bro (@BRO), or party permit from the child's "
  'scope (repeatable)'
)
SHARE_HELP = (
  'give the child read access to an artifact ref this session can itself read (repeatable)'
)
INTO_HELP = "base the child's workspace on this git ref instead of the summoner's workspace HEAD"
DETACH_HELP = 'print the request id and exit after sending; collect it with summon check'
TALK_HELP = (
  'widen the child quest chat rights from worker.say; comma-separated values from '
  'requester.say, requester.question, worker.say, worker.question'
)


def manual_launch_command(request_id: str, target: str) -> str:
  """the ride command that launches a manual summon's child session — what the
  summoner relays to the user along with the token (the request id)."""
  runtime = os.environ.get(RUNTIME_ENV)
  if runtime is None:
    raise RuntimeError(f'{RUNTIME_ENV} is missing from the managed session environment')
  executable = shlex.quote(f'{runtime}/venv/bin/ride')
  return f'{executable} along --summoned {request_id} {target}'


def encode_may_summon(targets: Collection[str]) -> str:
  """an effective summon allow-list as the `MAY_SUMMON_ENV` value: the names
  sorted and comma-joined, empty for a run that may summon nothing."""
  return ','.join(sorted(set(targets)))


def encode_permits(permits: Collection[str]) -> str:
  """An effective permit set as the `PERMITS_ENV` value."""
  values = set(permits)
  unknown = sorted(values - PARTY_PERMITS)
  if unknown:
    raise ValueError(f'unknown permit(s): {", ".join(unknown)}')
  return ','.join(sorted(values))


def parse_talk(values: Optional[list[str]]) -> Optional[list[str]]:
  """Validate comma-separated talk flags and return their request-field shape."""
  if values is None:
    return None
  from bro.broker.brotocol import TALK_RIGHTS

  rights = [right for value in values for right in value.split(',')]
  if any(not right for right in rights):
    raise ValueError('talk contains an empty right')
  unknown = sorted(set(rights) - TALK_RIGHTS)
  if unknown:
    raise ValueError(f'unknown talk right(s): {", ".join(unknown)}')
  if len(rights) != len(set(rights)):
    raise ValueError('talk contains a duplicate right')
  return rights


def summoned_child_env(
  may_summon: Collection[str],
  permits: Collection[str],
  summoner: Optional[dict[str, Any]],
) -> dict[str, str]:
  """the env that makes a run a summoned child, written by the surface that
  launches it: the mark, the child's own effective allow-list, and its
  summoner's attribution when there is one."""
  env = {
    SUMMONED_ENV: '1',
    MAY_SUMMON_ENV: encode_may_summon(may_summon),
    PERMITS_ENV: encode_permits(permits),
  }
  if summoner is not None:
    env[SUMMONER_ENV] = json.dumps(summoner, ensure_ascii=False, separators=(',', ':'))
  return env


def summoned() -> bool:
  """whether this run is a summoned child — a summoner is blocked on the result
  it owes back through the `answer` tool."""
  return os.environ.get(SUMMONED_ENV) is not None


def may_summon() -> Optional[tuple[str, ...]]:
  """the bros this run may summon, as its launch fixed them — the empty tuple
  when it may summon none, and None when it was launched by a surface that
  publishes no list. Read-only: the host authorizes against its own copy, so
  nothing here can widen it."""
  raw = os.environ.get(MAY_SUMMON_ENV)
  if raw is None:
    return None
  return tuple(name for name in raw.split(',') if len(name) > 0)


def talk() -> Optional[tuple[str, ...]]:
  """the chat rights fixed for this run's own quest — empty when it is mute,
  and None when its launcher published no talk."""
  from bro.broker.brotocol import TALK_ENV, decode_talk

  raw = os.environ.get(TALK_ENV)
  if raw is None:
    return None
  return tuple(sorted(decode_talk(raw)))


def permits() -> Optional[tuple[str, ...]]:
  """The party permits fixed by this run's launcher."""
  raw = os.environ.get(PERMITS_ENV)
  if raw is None:
    return None
  values = tuple(name for name in raw.split(',') if name)
  unknown = sorted(set(values) - PARTY_PERMITS)
  if unknown:
    raise ValueError(f'{PERMITS_ENV} carries unknown permit(s): {", ".join(unknown)}')
  return values


def effective_may_summon() -> tuple[str, ...]:
  """the summon allow-list as a rendering fact (`#may_summon`): the published
  list, with an unpublished one collapsed to empty — a run whose launcher
  published no list should plan no delegation."""
  return may_summon() or ()


def party_member() -> Optional[str]:
  """The member name when this session joined an existing party."""
  return os.environ.get(PARTY_MEMBER_ENV) or None


def summoned_by_from_env() -> Optional[dict[str, Any]]:
  """this run's `summoned_by` trail provenance, read off `SUMMONER_ENV` — None
  when the run was not summoned or its summoner published no trail yet.

  Consumed on read: tool subprocesses inherit this process's environment, so a
  nested in-process run inside the summoned child's container must not re-stamp
  the parent's summoned_by on its own trail — it was not itself summoned."""
  raw = os.environ.pop(SUMMONER_ENV, None)
  if raw is None:
    return None
  summoned_by = json.loads(raw)
  if not isinstance(summoned_by, dict):
    raise ValueError(f'{SUMMONER_ENV} must be a JSON object')
  keys = set(summoned_by)
  if not {'trail_id'}.issubset(keys) or not keys.issubset({'trail_id', 'step_id', 'index'}):
    raise ValueError(f'{SUMMONER_ENV} has an invalid summoned_by shape')
  trail_id = summoned_by['trail_id']
  step_id = summoned_by.get('step_id')
  index = summoned_by.get('index')
  if (
    not isinstance(trail_id, str)
    or len(trail_id) == 0
    or (
      step_id is not None
      and (not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0)
    )
    or (
      index is not None
      and (step_id is None or not isinstance(index, int) or isinstance(index, bool) or index < 0)
    )
  ):
    raise ValueError(f'{SUMMONER_ENV} has an invalid summoned_by shape')
  return summoned_by


class SummonError(Exception):
  """a summon that produced no usable answer: denied, malformed, raised, failed,
  or its result never arrived. The message is the operator-facing reason."""


class _BrokerReadTimeout(SummonError):
  pass


def _open_client() -> 'Client':
  from bro.broker.client import CHANNEL_ENV, Client

  client = Client.from_env()
  if client is None:
    raise SummonError(f'no broker channel ({CHANNEL_ENV} unset); summon needs a session channel')
  return client


def _payload(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  share: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  party: Optional[str] = None,
  isolation: Optional[str] = None,
  talk: Optional[list[str]] = None,
  manual: bool = False,
) -> dict[str, Any]:
  payload: dict[str, Any] = {'target': target, 'prompt': prompt}
  if timeout is not None:
    payload['timeout'] = timeout
  if into is not None:
    payload['into'] = into
  if hold is not None:
    payload['hold'] = hold
  if step_id is not None:
    payload['step_id'] = step_id
  if index is not None:
    payload['index'] = index
  if grant is not None:
    payload['grant'] = list(grant)
  if revoke is not None:
    payload['revoke'] = list(revoke)
  if share is not None:
    payload['share'] = list(share)
  if llm is not None:
    payload['llm'] = llm
  if harness is not None:
    payload['harness'] = harness
  if party is not None:
    payload['party'] = party
  if isolation is not None:
    payload['isolation'] = isolation
  if talk is not None:
    payload['talk'] = list(talk)
  if manual:
    payload['manual'] = True
  return payload


def _send_summon(client: 'Client', payload: dict[str, Any]) -> 'Message':
  from bro.broker.brotocol import ProtocolError

  try:
    return client.send(SUMMON, payload)
  except ProtocolError:
    raise SummonError('prompt too large; share an artifact instead') from None


@contextlib.contextmanager
def _connection(client: Optional['Client']) -> Generator['Client']:
  """the channel client a call runs on: a caller-owned one passed through with
  its lifecycle left alone, or a fresh one closed on the way out."""
  if client is not None:
    yield client
    return
  with _open_client() as owned:
    yield owned


def _trails_hint(trail_id: Optional[str]) -> str:
  if trail_id is not None:
    return f'inspect the run with `rewind show {trail_id}`'
  return 'the run has announced no trail'


def _interpret_payload(payload: dict[str, Any], trail_id: Optional[str]) -> str:
  """Turn a summon result payload into its answer or an operator-facing error."""
  outcome = payload.get('outcome')
  if outcome == 'ok':
    value = payload.get('value')
    return value if value is not None else ''
  if outcome == 'denied':
    raise SummonError(str(payload.get('error', payload)))
  detail = payload.get('detail')
  detail = detail if isinstance(detail, dict) else {}
  reason = detail.get('reason')
  error = payload.get('error')
  if reason in ('raised', 'error'):
    raise SummonError(f'summon {reason}: {error}')
  parts = [f'summon failed ({reason})']
  diagnostic = error if error is not None else detail.get('output_tail')
  if diagnostic is not None and len(str(diagnostic).strip()) > 0:
    parts.append(str(diagnostic).strip())
  parts.append(_trails_hint(trail_id))
  raise SummonError('; '.join(parts))


def _interpret_result(message: 'Message', trail_id: Optional[str]) -> str:
  return _interpret_payload(message.payload, trail_id)


def _read_value(
  client: 'Client', kind: str, args: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
  try:
    result = client.call(kind, args, timeout)
  except TimeoutError:
    raise _BrokerReadTimeout(f'no reply to broker {kind!r} read within {timeout:.0f}s') from None
  except ConnectionError as error:
    raise SummonError(f'broker channel closed during {kind!r} read: {error}') from None
  payload = result.payload
  if payload.get('outcome') != 'ok':
    raise SummonError(str(payload.get('error', payload)))
  value = payload.get('value')
  if not isinstance(value, dict):
    raise SummonError(f'broker {kind!r} read returned a malformed value: {value!r}')
  return value


def _query_quest(
  client: 'Client',
  request_id: str,
  *,
  wait_seconds: float = 0,
  since: Optional[int] = None,
  read_timeout: Optional[float] = None,
) -> dict[str, Any]:
  from bro.broker.dispatcher import QUERY

  args: dict[str, Any] = {'id': request_id}
  if wait_seconds > 0:
    args['wait'] = wait_seconds
  if since is not None:
    args['since'] = since
  value = _read_value(
    client,
    QUERY,
    args,
    timeout=(
      max(ACCEPT_TIMEOUT, wait_seconds + ACCEPT_TIMEOUT) if read_timeout is None else read_timeout
    ),
  )
  quest = value.get('quest')
  if not isinstance(quest, dict):
    raise SummonError(f'query for {request_id!r} returned no quest record')
  return quest


def _summon_answer(quest: dict[str, Any]) -> Optional[str]:
  request_id = quest.get('id')
  if quest.get('kind') != SUMMON:
    raise SummonError(f'quest {request_id!r} is not a summon')
  state = quest.get('state')
  if state in ('accepted', 'started'):
    return None
  trail_id = quest.get('trail_id')
  if state == 'evicted' or quest.get('result_evicted') is True:
    raise SummonError(f'summon result is no longer retained; {_trails_hint(trail_id)}')
  if state not in ('ended', 'denied'):
    raise SummonError(f'summon quest {request_id!r} has unknown state {state!r}')
  result = quest.get('result')
  if not isinstance(result, dict):
    raise SummonError(
      f'summon quest {request_id!r} has no retained result; {_trails_hint(trail_id)}'
    )
  return _interpret_payload(result, trail_id if isinstance(trail_id, str) else None)


def _require_live_summon(quest: dict[str, Any]) -> None:
  answer = _summon_answer(quest)
  if answer is not None:
    raise SummonError(f'summon quest {quest.get("id")!r} already ended successfully')


def _text_from_payload(payload: Any) -> str:
  if (
    not isinstance(payload, dict)
    or set(payload) != {'text'}
    or not isinstance(payload['text'], str)
  ):
    raise SummonError(f'summon chat carried a malformed text payload: {payload!r}')
  return payload['text']


def _text_from_entry(entry: dict[str, Any]) -> str:
  return _text_from_payload(entry.get('head'))


def _bounded_text(text: str) -> str:
  from bro.broker.journal import MESSAGE_HEAD_BYTES

  if not isinstance(text, str) or not text:
    raise ValueError('summon chat text must be a non-empty string')

  def payload_size(candidate: str) -> int:
    return len(
      json.dumps({'text': candidate}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    )

  if payload_size(text) <= MESSAGE_HEAD_BYTES:
    return text
  lower = 0
  upper = len(text)
  while lower < upper:
    middle = (lower + upper + 1) // 2
    if payload_size(text[:middle]) <= MESSAGE_HEAD_BYTES:
      lower = middle
    else:
      upper = middle - 1
  return text[:lower]


@dataclass(frozen=True)
class SummonQuestion:
  id: str
  text: str
  request_id: Optional[str] = None


def _question_from_message(message: 'Message') -> SummonQuestion:
  if message.id is None:
    raise SummonError('summon question carried no id')
  return SummonQuestion(message.id, _text_from_payload(message.payload), message.quest_id)


def _await_answer(
  client: 'Client',
  request: 'Message',
  *,
  timeout: float,
  on_started: Optional[Callable[[str], None]] = None,
  silence_timeout: Optional[float] = None,
) -> str | SummonQuestion:
  """Wait for a result or child question, consulting the journal on wire silence."""
  from bro.broker.brotocol import Tag

  trail_id: Optional[str] = None

  def _interim(message: 'Message') -> None:
    nonlocal trail_id
    if message.type == Tag.MESSAGE:
      log.info('summon says %s', _single_line(_text_from_payload(message.payload)))
      return
    if message.type != Tag.MARK or message.payload.get('transition') != 'trail':
      return
    value = message.payload.get('trail_id')
    if not isinstance(value, str):
      return
    trail_id = value
    if on_started is not None:
      on_started(value)

  silence = timeout if silence_timeout is None else silence_timeout
  while True:
    try:
      result = client.await_reply(
        request,
        silence,
        on_interim=_interim,
        timeout_after_interim=silence,
        until=lambda message: message.type == Tag.MESSAGE and message.id is not None,
      )
    except TimeoutError:
      quest = _query_quest(client, request.quest_id)
      status = _summon_status(quest)
      if status.answer is not None:
        return status.answer
      if status.question is not None:
        return status.question
      queried_trail = quest.get('trail_id')
      if isinstance(queried_trail, str) and queried_trail != trail_id:
        trail_id = queried_trail
        if on_started is not None:
          on_started(queried_trail)
      continue
    except ConnectionError as error:
      raise SummonError(f'broker channel closed awaiting the summon result: {error}') from None
    if result.type == Tag.MESSAGE:
      return _question_from_message(result)
    return _interpret_result(result, trail_id)


def open_client() -> 'Client':
  """Open a channel client whose lifecycle the caller owns."""
  return _open_client()


def summon_and_wait(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  share: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  party: Optional[str] = None,
  isolation: Optional[str] = None,
  talk: Optional[list[str]] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
  on_sent: Optional[Callable[[str], None]] = None,
  client: Optional['Client'] = None,
  silence_timeout: Optional[float] = None,
) -> str | SummonQuestion:
  """Send one summon and wait for its answer or first child question."""
  payload = _payload(
    target,
    prompt,
    timeout=timeout,
    into=into,
    hold=hold,
    step_id=step_id,
    index=index,
    grant=grant,
    revoke=revoke,
    share=share,
    llm=llm,
    harness=harness,
    party=party,
    isolation=isolation,
    talk=talk,
  )
  with _connection(client) as connection:
    request = _send_summon(connection, payload)
    if on_sent is not None:
      on_sent(request.quest_id)
    return _await_answer(
      connection,
      request,
      timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
      silence_timeout=silence_timeout,
    )


def _await_acceptance(client: 'Client', request: 'Message') -> None:
  """Require the first correlated message to accept the quest or explain its failure."""
  from bro.broker.brotocol import Tag

  try:
    first = client.await_any(request, ACCEPT_TIMEOUT)
  except TimeoutError:
    raise SummonError(f'no acceptance within {ACCEPT_TIMEOUT:.0f}s of the summon request') from None
  except ConnectionError as error:
    raise SummonError(f'broker channel closed awaiting summon acceptance: {error}') from None
  if first.type == Tag.RESULT:
    _interpret_result(first, None)
    raise SummonError(f'summon ended before acceptance: {first.payload}')
  if first.type != Tag.MARK or first.payload.get('transition') != 'accepted':
    raise SummonError(f'unexpected first summon reply: {first.payload}')


def summon_detached(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  share: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  party: Optional[str] = None,
  isolation: Optional[str] = None,
  talk: Optional[list[str]] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
) -> str:
  """Send one summon and return its accepted quest id."""
  payload = _payload(
    target,
    prompt,
    timeout=timeout,
    into=into,
    hold=hold,
    step_id=step_id,
    index=index,
    grant=grant,
    revoke=revoke,
    share=share,
    llm=llm,
    harness=harness,
    party=party,
    isolation=isolation,
    talk=talk,
  )
  with _open_client() as client:
    request = _send_summon(client, payload)
    _await_acceptance(client, request)
    return request.quest_id


def summon_manual(
  target: str,
  prompt: str,
  *,
  into: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  talk: Optional[list[str]] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
) -> str:
  """Register a manual summon and return its accepted launch token."""
  payload = _payload(
    target,
    prompt,
    into=into,
    grant=grant,
    revoke=revoke,
    talk=talk,
    manual=True,
    step_id=step_id,
    index=index,
  )
  with _open_client() as client:
    request = _send_summon(client, payload)
    _await_acceptance(client, request)
    return request.quest_id


@dataclass(frozen=True)
class SummonStatus:
  """A repeatable journal-backed quest-chat check."""

  pending: bool
  answer: Optional[str] = None
  trail_id: Optional[str] = None
  request_id: Optional[str] = None
  talk: tuple[str, ...] = ()
  pending_questions: tuple[dict[str, Any], ...] = ()
  messages: tuple[dict[str, Any], ...] = ()
  messages_truncated: bool = False
  question: Optional[SummonQuestion] = None
  chat_seq: int = 0


def _caller_end(quest: dict[str, Any], request_id: str) -> Optional['End']:
  from bro.broker.client import QUEST_ENV

  own_quest = os.environ.get(QUEST_ENV)
  if own_quest is None:
    raise SummonError(f'{QUEST_ENV} is missing from the session environment')
  if own_quest == request_id:
    return 'worker'
  return 'requester' if quest.get('parent') == own_quest else None


def _summon_status(quest: dict[str, Any], *, caller: Optional['End'] = 'requester') -> SummonStatus:
  answer = _summon_answer(quest)
  trail_id = quest.get('trail_id')
  request_id = quest.get('id')
  talk = quest.get('talk')
  pending_questions = quest.get('pending')
  messages = quest.get('messages')
  messages_truncated = quest.get('messages_truncated', False)
  chat_seq = quest.get('chat_seq')
  if not isinstance(request_id, str):
    raise SummonError('summon query returned no request id')
  if not isinstance(talk, list) or not all(isinstance(right, str) for right in talk):
    raise SummonError('summon query returned malformed talk rights')
  _talk_for_quest(quest)
  if not isinstance(pending_questions, list) or not all(
    isinstance(entry, dict) for entry in pending_questions
  ):
    raise SummonError('summon query returned malformed pending questions')
  if not isinstance(messages, list) or not all(isinstance(entry, dict) for entry in messages):
    raise SummonError('summon query returned a malformed chat tail')
  if not isinstance(messages_truncated, bool):
    raise SummonError('summon query returned a malformed chat truncation marker')
  if not isinstance(chat_seq, int) or isinstance(chat_seq, bool):
    raise SummonError('summon query returned a malformed chat sequence')
  incoming = (
    [] if caller is None else [entry for entry in pending_questions if entry.get('from') != caller]
  )
  question = None
  if incoming:
    latest = incoming[-1]
    question_id = latest.get('id')
    if not isinstance(question_id, str):
      raise SummonError('pending summon question carried no id')
    question = SummonQuestion(question_id, _text_from_entry(latest), request_id)
  return SummonStatus(
    pending=answer is None,
    answer=answer,
    trail_id=trail_id if isinstance(trail_id, str) else None,
    request_id=request_id,
    talk=tuple(talk),
    pending_questions=tuple(pending_questions),
    messages=tuple(messages),
    messages_truncated=messages_truncated,
    question=question,
    chat_seq=chat_seq,
  )


def _resolve_request_id(request_id: Optional[str]) -> str:
  if request_id is not None:
    return request_id
  from bro.broker.client import QUEST_ENV

  own_quest = os.environ.get(QUEST_ENV)
  if own_quest is None:
    raise SummonError(f'no quest given and {QUEST_ENV} is unset')
  return own_quest


def check_summon(request_id: Optional[str] = None) -> SummonStatus:
  """Read one summon quest without consuming its retained result or chat."""
  resolved = _resolve_request_id(request_id)
  with _open_client() as client:
    quest = _query_quest(client, resolved)
  return _summon_status(quest, caller=_caller_end(quest, resolved))


def wait_summon(
  request_id: Optional[str] = None,
  *,
  timeout: Optional[float] = None,
  client: Optional['Client'] = None,
  wait_seconds: Optional[float] = None,
) -> SummonStatus:
  """Long-poll a summon quest until terminal or its chat next changes."""
  if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
    raise SummonError('timeout must be a finite positive number')
  interval = wait_seconds if wait_seconds is not None else READ_WAIT_SECONDS
  if interval <= 0:
    raise SummonError('query wait must be positive')
  resolved = _resolve_request_id(request_id)
  deadline = None if timeout is None else time.monotonic() + timeout
  with _connection(client) as connection:
    initial_timeout = None if deadline is None else deadline - time.monotonic()
    quest = _query_quest(connection, resolved, read_timeout=initial_timeout)
    caller = _caller_end(quest, resolved)
    status = _summon_status(quest, caller=caller)
    if not status.pending or status.question is not None:
      return status
    cursor = status.chat_seq
    while True:
      remaining = None if deadline is None else deadline - time.monotonic()
      if remaining is not None and remaining <= 0:
        return status
      poll_seconds = interval if remaining is None else min(interval, remaining)
      try:
        quest = _query_quest(
          connection,
          resolved,
          wait_seconds=poll_seconds,
          since=cursor,
          read_timeout=remaining,
        )
      except _BrokerReadTimeout:
        if deadline is None or time.monotonic() < deadline:
          raise
        return status
      status = _summon_status(quest, caller=caller)
      if not status.pending or status.chat_seq > cursor:
        return status


@dataclass(frozen=True)
class SayStatus:
  state: str
  request_id: str
  question_id: Optional[str] = None
  answer: Optional[str] = None


def _talk_for_quest(quest: dict[str, Any]) -> 'Talk':
  from bro.broker.brotocol import TALK_RIGHTS

  values = quest.get('talk')
  if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
    raise SummonError('summon query returned malformed talk rights')
  talk = frozenset(values)
  if len(talk) != len(values) or not talk.issubset(TALK_RIGHTS):
    raise SummonError('summon query returned invalid talk rights')
  return cast('Talk', talk)


def _reply_from_tail(quest: dict[str, Any], question_id: str) -> Optional[str]:
  messages = quest.get('messages')
  if not isinstance(messages, list) or not all(isinstance(entry, dict) for entry in messages):
    raise SummonError('summon query returned a malformed chat tail')
  for entry in reversed(messages):
    transition = entry.get('transition')
    if transition not in ('message', 'refused'):
      raise SummonError(f'summon chat tail carried an unknown transition: {transition!r}')
    if transition == 'refused' and entry.get('id') == question_id:
      reason = entry.get('reason')
      if not isinstance(reason, str):
        raise SummonError('refused summon chat entry carried no reason')
      raise SummonError(reason)
    if transition == 'message' and entry.get('reply_to') == question_id:
      return _text_from_entry(entry)
  return None


def say(
  text: str,
  request_id: Optional[str] = None,
  *,
  reply_to: Optional[str] = None,
  wait: Optional[float] = None,
  client: Optional['Client'] = None,
) -> SayStatus:
  """Send summon chat traffic and optionally wait for the question's reply."""
  from bro.broker import brotocol

  if wait is not None and (wait <= 0 or math.isnan(wait)):
    raise SummonError('wait must be a positive number of seconds')
  resolved = _resolve_request_id(request_id)
  with _connection(client) as connection:
    quest = _query_quest(connection, resolved)
    sender = _caller_end(quest, resolved)
    if sender is None:
      raise SummonError(f'quest {resolved!r} is not a direct child of this session')
    _require_live_summon(quest)
    try:
      candidate = brotocol.message(
        resolved,
        {'text': _bounded_text(text)},
        id='question' if wait is not None else None,
        reply_to=reply_to,
      )
    except brotocol.ProtocolError as error:
      raise SummonError(str(error)) from error
    talk = _talk_for_quest(quest)
    if sender == 'worker':
      from bro.broker.client import talk_from_env

      published_talk = talk_from_env()
      if published_talk is None:
        raise SummonError(f'{brotocol.TALK_ENV} is missing from the session environment')
      talk = published_talk
    if not brotocol.message_allowed(talk, sender, candidate):
      source = brotocol.TALK_ENV if sender == 'worker' else f'query {resolved}'
      raise SummonError(f'{source} forbids this summon chat move')
    try:
      sent = connection.message(
        resolved,
        candidate.payload,
        reply_to=reply_to,
        question=wait is not None,
      )
    except (PermissionError, brotocol.ProtocolError) as error:
      raise SummonError(str(error)) from error
    if sent.id is None:
      return SayStatus('accepted', resolved)
    question_id = sent.id
    assert wait is not None
    deadline = None if math.isinf(wait) else time.monotonic() + wait
    initial_remaining = None if deadline is None else deadline - time.monotonic()
    if initial_remaining is not None and initial_remaining <= 0:
      return SayStatus('question', resolved, question_id=question_id)
    try:
      current = _query_quest(connection, resolved, read_timeout=initial_remaining)
    except _BrokerReadTimeout:
      if deadline is None or time.monotonic() < deadline:
        raise
      return SayStatus('question', resolved, question_id=question_id)
    while True:
      answer = _reply_from_tail(current, question_id)
      if answer is not None:
        return SayStatus('completed', resolved, question_id=question_id, answer=answer)
      _require_live_summon(current)
      remaining = None if deadline is None else deadline - time.monotonic()
      if remaining is not None and remaining <= 0:
        return SayStatus('question', resolved, question_id=question_id)
      poll_seconds = READ_WAIT_SECONDS if remaining is None else min(READ_WAIT_SECONDS, remaining)
      try:
        current = _query_quest(
          connection,
          resolved,
          wait_seconds=poll_seconds,
          since=current.get('chat_seq', 0),
          read_timeout=remaining,
        )
      except _BrokerReadTimeout:
        if deadline is None or time.monotonic() < deadline:
          raise
        return SayStatus('question', resolved, question_id=question_id)


def list_summons() -> dict[str, Any]:
  """Return every caller-visible retained summon record, live first."""
  from bro.broker.dispatcher import QUERY

  quests: list[dict[str, Any]] = []
  cursor: Optional[str] = None
  with _open_client() as client:
    while True:
      args = {} if cursor is None else {'cursor': cursor}
      value = _read_value(client, QUERY, args, timeout=ACCEPT_TIMEOUT)
      page = value.get('quests')
      if not isinstance(page, list) or not all(isinstance(quest, dict) for quest in page):
        raise SummonError('query listing returned malformed quest records')
      quests.extend(quest for quest in page if quest.get('kind') == SUMMON)
      cursor = value.get('cursor')
      if cursor is None:
        break
      if not isinstance(cursor, str):
        raise SummonError('query listing returned a malformed cursor')
  return {'quests': quests}


def _single_line(text: str) -> str:
  return ''.join(
    character if character.isprintable() else repr(character)[1:-1] for character in text
  )


def _request_clause(event: dict[str, Any], own_quest: str) -> str:
  args = event.get('args')
  parent = event.get('parent')
  if not isinstance(args, dict) or not isinstance(parent, str):
    raise SummonError('events read returned a malformed summon record')
  clause = f'request {event.get("quest")}'
  target = args.get('target')
  if isinstance(target, str):
    clause += f' to {target}'
  elif event.get('transition') != 'denied':
    raise SummonError('events read returned an accepted summon without a target')
  if parent != own_quest:
    clause += f', summoned by request {parent}'
  return clause


def _chat_event_line(event: dict[str, Any], own_quest: str) -> Optional[str]:
  transition = event.get('transition')
  is_own_quest = event.get('quest') == own_quest
  sender = event.get('from')
  other_end = sender == ('requester' if is_own_quest else 'worker')
  if transition == 'message' and not other_end:
    return None
  if transition == 'listening':
    if is_own_quest:
      return None
    return _single_line(f'summon listening ({_request_clause(event, own_quest)})')
  if transition not in ('message', 'refused'):
    return None
  actor = 'summoner' if is_own_quest else 'summon'
  if transition == 'refused':
    reason = event.get('reason')
    if not isinstance(reason, str):
      raise SummonError('refused summon event carried no reason')
    head = f'{actor} refused {reason}: {_text_from_entry(event)}'
  elif event.get('id') is not None:
    head = f'{actor} asks {_text_from_entry(event)}'
  elif event.get('reply_to') is not None:
    head = f'{actor} replies {_text_from_entry(event)}'
  else:
    head = f'{actor} says {_text_from_entry(event)}'
  details = []
  if not is_own_quest:
    details.append(_request_clause(event, own_quest))
  if event.get('id') is not None:
    details.append(f'question {event["id"]}')
  if event.get('reply_to') is not None:
    details.append(f'to {event["reply_to"]}')
  return _single_line(head if not details else f'{head} ({", ".join(details)})')


def _event_line(event: dict[str, Any], own_quest: str) -> str:
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
  return _single_line(f'{head} ({_request_clause(event, own_quest)})')


def watch_summons(wait_seconds: float = READ_WAIT_SECONDS) -> Generator[str]:
  """Yield ordered summon journal transitions from the moment the watch is armed."""
  if wait_seconds <= 0:
    raise SummonError('events wait must be positive')
  from bro.broker.client import QUEST_ENV
  from bro.broker.dispatcher import EVENTS

  with _open_client() as client:
    own_quest = os.environ.get(QUEST_ENV)
    if own_quest is None:
      raise SummonError(
        f'broker channel present but {QUEST_ENV} unset; '
        'the launch did not name the quest this session answers'
      )
    baseline = _read_value(client, EVENTS, {}, timeout=ACCEPT_TIMEOUT)
    head = baseline.get('head')
    if not isinstance(head, int) or isinstance(head, bool):
      raise SummonError('events arm returned a malformed head')
    cursor = head
    while True:
      try:
        value = _read_value(
          client,
          EVENTS,
          {'after': cursor, 'wait': wait_seconds},
          timeout=max(ACCEPT_TIMEOUT, wait_seconds + ACCEPT_TIMEOUT),
        )
      except SummonError as error:
        if not str(error).startswith('events gap:'):
          raise
        baseline = _read_value(client, EVENTS, {}, timeout=ACCEPT_TIMEOUT)
        head = baseline.get('head')
        if not isinstance(head, int) or isinstance(head, bool):
          raise SummonError('events re-arm returned a malformed head') from error
        cursor = head
        yield _single_line(f'summon watch gap: {error}; re-armed at {head}')
        continue
      events = value.get('events')
      if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise SummonError('events read returned malformed records')
      for event in events:
        sequence = event.get('seq')
        if not isinstance(sequence, int) or isinstance(sequence, bool):
          raise SummonError('events read returned a malformed sequence')
        cursor = max(cursor, sequence)
        if event.get('kind') != SUMMON:
          continue
        chat_line = _chat_event_line(event, own_quest)
        if chat_line is not None:
          yield chat_line
        elif event.get('transition') not in ('message', 'refused', 'listening'):
          yield _event_line(event, own_quest)


def relay_summon(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  share: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  party: Optional[str] = None,
  isolation: Optional[str] = None,
  talk: Optional[list[str]] = None,
  manual: bool = False,
) -> int:
  """send one summon and relay its outcome as a CLI would: the request id and
  the started trail id to stderr, the answer to stdout, any failure as an error
  log line. Returns the exit code — the blocking `summon` CLI mode, exposed for
  the self-contained blocking `summon` CLI. A blocking manual summon prints the
  launch command to relay once the host accepts (a denial fails right there),
  then waits on the same journal-backed quest as any other summon."""
  payload = _payload(
    target,
    prompt,
    timeout=timeout,
    into=into,
    hold=hold,
    grant=grant,
    revoke=revoke,
    share=share,
    llm=llm,
    harness=harness,
    party=party,
    isolation=isolation,
    talk=talk,
    manual=manual,
  )
  try:
    client = _open_client()
  except SummonError as e:
    log.error('%s', e)
    return 1
  with client:
    try:
      request = _send_summon(client, payload)
    except SummonError as error:
      log.error('%s', error)
      return 1
    log.info('summon request %s', request.quest_id)
    if manual:
      try:
        _await_acceptance(client, request)
      except SummonError as e:
        log.error('%s', e)
        return 1
      log.info('have the user run: %s', manual_launch_command(request.quest_id, target))
    effective = timeout if timeout is not None else DEFAULT_TIMEOUT
    return _relay(
      lambda: _await_answer(
        client,
        request,
        timeout=effective,
        on_started=lambda trail_id: log.info('summon started: trail %s', trail_id),
      )
    )


# --- CLI ------------------------------------------------------------------------


def _relay(await_answer: Callable[[], str | SummonQuestion]) -> int:
  try:
    result = await_answer()
  except SummonError as error:
    log.error('%s', error)
    return 1
  if isinstance(result, SummonQuestion):
    print(result.text)
    if result.request_id is None:
      raise RuntimeError('a blocking summon question carried no request id')
    log.info(
      "summon question %s; reply with `summon say %s '<text>' --reply-to %s`",
      result.id,
      result.request_id,
      result.id,
    )
    return QUESTION_EXIT_CODE
  print(result)
  return 0


def _list() -> int:
  try:
    status = list_summons()
  except SummonError as e:
    log.error('%s', e)
    return 1
  print(json.dumps(status, indent=2, ensure_ascii=False))
  return 0


def _watch() -> int:
  try:
    for event in watch_summons():
      print(event, flush=True)
  except SummonError as e:
    log.error('%s', e)
    return 1
  return 0


def status_view(status: SummonStatus) -> dict[str, Any]:
  if status.question is not None:
    state = 'question'
  elif status.pending:
    state = 'pending'
  else:
    state = 'completed'
  view: dict[str, Any] = {
    'state': state,
    'request_id': status.request_id,
    'talk': list(status.talk),
    'pending': list(status.pending_questions),
    'messages': list(status.messages),
  }
  if status.trail_id is not None:
    view['trail_id'] = status.trail_id
  if status.messages_truncated:
    view['messages_truncated'] = True
  if status.question is not None:
    view['question'] = {'id': status.question.id, 'text': status.question.text}
  if not status.pending:
    view['answer'] = status.answer
  return view


def _say(
  text: str,
  request_id: Optional[str],
  reply_to: Optional[str],
  wait: Optional[float],
) -> int:
  try:
    status = say(text, request_id, reply_to=reply_to, wait=wait)
  except (SummonError, ValueError) as error:
    log.error('%s', error)
    return 1
  if status.state == 'completed':
    print(status.answer)
    return 0
  if status.state == 'question':
    assert status.question_id is not None
    print(status.question_id)
    return QUESTION_EXIT_CODE
  return 0


def _check(request_id: Optional[str], wait: bool, timeout: Optional[float]) -> int:
  if timeout is not None and not wait:
    log.error('--timeout only bounds a wait; a plain check never blocks')
    return 1
  try:
    status = wait_summon(request_id, timeout=timeout) if wait else check_summon(request_id)
  except SummonError as error:
    log.error('%s', error)
    return 1
  if status.question is not None:
    print(json.dumps(status_view(status), indent=2, ensure_ascii=False))
    return QUESTION_EXIT_CODE
  if status.pending:
    print(json.dumps(status_view(status), indent=2, ensure_ascii=False))
    log.info('summon still running; %s', _trails_hint(status.trail_id))
    return PENDING_EXIT_CODE
  assert status.answer is not None
  print(status.answer)
  return 0


def main(argv: list[str]) -> Optional[int]:
  if len(argv) > 1 and argv[1] == 'say':
    parser = base_args.Parser(
      prog='summon say',
      description="send a message to a child quest, or to this session's summoner when no quest is given",
    )
    parser.add_argument(
      'request_id', nargs='?', help='child quest id; omit for this session’s quest'
    )
    parser.add_argument('text', help='message text')
    parser.add_argument('--reply-to', help='question id this message answers')
    parser.add_argument(
      '--wait',
      nargs='?',
      const=math.inf,
      type=float,
      metavar='SECONDS',
      help=f'ask a question and wait for its reply; a timed-out question exits {QUESTION_EXIT_CODE}',
    )
    return _say(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'list':
    parser = base_args.Parser(
      prog='summon list',
      description="list this session's retained summon journal records, live first; "
      'each id is a repeatable reattach handle for `summon check`',
    )
    return _list(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'watch':
    parser = base_args.Parser(
      prog='summon watch',
      description="stream the ordered transitions of every summon in this session's subtree "
      '— its own and the ones its summoned bros make in turn. '
      'Runs until killed; what is already in flight when it starts is the baseline',
    )
    return _watch(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'check':
    parser = base_args.Parser(
      prog='summon check',
      description='check on a detached or interrupted summon by its request id: '
      'print the answer if the result is in, otherwise report `still running` and '
      f'exit {PENDING_EXIT_CODE} without blocking; --wait long-polls the same repeatable read',
    )
    parser.add_argument(
      'request_id',
      nargs='?',
      help="child request id; omit to check this session's own summon quest",
    )
    parser.add_argument(
      '--wait',
      action='store_true',
      help='block until the result or next chat message arrives, or --timeout passes; '
      'concurrent waits and later checks are safe because journal reads are non-destructive',
    )
    parser.add_argument(
      '--timeout',
      type=float,
      help='with --wait: maximum seconds to wait before exiting pending; '
      'omitted waits until terminal',
    )
    return _check(**parser.parse(argv[1:]))
  parser = base_args.Parser(
    prog='summon',
    description='summon a bro over the session channel; use `summon check` to reattach '
    'to a request and `summon list` to rediscover request ids',
  )
  parser.add_argument('target', help='bro to summon')
  parser.add_argument('prompt', help='request the summoned bro answers')
  add_llm_flags(parser, effort_help=EFFORT_HELP, fast_help=FAST_HELP)
  parser.add_argument('--grant', action='append', default=None, metavar='NAME', help=GRANT_HELP)
  parser.add_argument('--revoke', action='append', default=None, metavar='NAME', help=REVOKE_HELP)
  parser.add_argument('--share', action='append', default=None, metavar='REF', help=SHARE_HELP)
  parser.add_argument(
    '--talk', action='append', default=None, metavar='RIGHT[,RIGHT]', help=TALK_HELP
  )
  parser.add_argument('--into', metavar='REF', help=INTO_HELP)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP)
  parser.add_argument('--harness', default=None, help=HARNESS_HELP)
  parser.add_argument(
    '--timeout',
    type=float,
    metavar='SECONDS',
    help=f'seconds before the host kills the child (default: {DEFAULT_TIMEOUT:.0f})',
  )
  placement = parser.add_mutually_exclusive_group()
  placement.add_argument(
    '--start',
    dest='party',
    action='store_const',
    const='start',
    help='start a party, choosing the first permitted isolation',
  )
  placement.add_argument(
    '--join',
    dest='party',
    action='store_const',
    const='join',
    help='join the summoner’s party (requires :party.join)',
  )
  placement.add_argument(
    '--boxed',
    dest='isolation',
    action='store_const',
    const='boxed',
    help='start a boxed party (requires :party.start.boxed)',
  )
  placement.add_argument(
    '--unboxed',
    dest='isolation',
    action='store_const',
    const='unboxed',
    help='start an unboxed party (requires :party.start.unboxed)',
  )
  parser.add_argument('--manual', action='store_true', help=MANUAL_HELP)
  parser.add_argument('--detach', action='store_true', help=DETACH_HELP)
  args = parser.parse(argv)
  try:
    canonicalize(args, selection_from_args(args))
    args['talk'] = parse_talk(args['talk'])
  except (LLMSelectionError, ValueError) as error:
    log.error('%s', error)
    return 1
  if args['party'] == 'join' and args['into'] is not None:
    log.error('--join shares the summoner’s tree; drop --into')
    return 1
  if args['manual']:
    launch_owned = {
      '--timeout': args['timeout'],
      '--hold': args['hold'],
      '--harness': args['harness'],
      '--llm': args['llm'],
      '--start/--join': args['party'],
      '--boxed/--unboxed': args['isolation'],
    }
    passed = sorted(flag for flag, value in launch_owned.items() if value is not None)
    if len(passed) > 0:
      log.error("a manual summon's launch owns %s; drop the flag(s)", ', '.join(passed))
      return 1
    if args['share'] is not None:
      log.error("a manual summon's workspace is not launched by summon control; drop --share")
      return 1
  if args['detach']:
    try:
      if args['manual']:
        request_id = summon_manual(
          args['target'],
          args['prompt'],
          into=args['into'],
          grant=args['grant'],
          revoke=args['revoke'],
          talk=args['talk'],
        )
        log.info('have the user run: %s', manual_launch_command(request_id, args['target']))
      else:
        request_id = summon_detached(
          args['target'],
          args['prompt'],
          timeout=args['timeout'],
          into=args['into'],
          hold=args['hold'],
          grant=args['grant'],
          revoke=args['revoke'],
          share=args['share'],
          llm=args['llm'],
          harness=args['harness'],
          party=args['party'],
          isolation=args['isolation'],
          talk=args['talk'],
        )
    except SummonError as error:
      log.error('%s', error)
      return 1
    print(request_id)
    return 0
  return relay_summon(
    args['target'],
    args['prompt'],
    timeout=args['timeout'],
    into=args['into'],
    hold=args['hold'],
    grant=args['grant'],
    revoke=args['revoke'],
    share=args['share'],
    llm=args['llm'],
    harness=args['harness'],
    party=args['party'],
    isolation=args['isolation'],
    talk=args['talk'],
    manual=args['manual'],
  )
