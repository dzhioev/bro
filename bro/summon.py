"""summon — request another bro through the session broker.

The module owns the summon request shape and every peer-side surface: blocking,
detached, manual, query-backed check/list, and the journal event watch.
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
import os
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import bro.base.args as base_args
from bro.base import log
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
  from bro.broker.brotocol import Message
  from bro.broker.client import Client

__cli_name__ = 'summon'

SUMMON = 'summon'  # the kind a summon request names
SUMMONER_ENV = 'RIDE_SUMMONER'
# marks a run as a summoned child, written by the surface that launches it
SUMMONED_ENV = 'RIDE_SUMMONED'
# carries a run's own effective summon allow-list into it, written by the surface
# that launches the run: a session root's at launch, a summoned child's at its spawn
MAY_SUMMON_ENV = 'RIDE_MAY_SUMMON'
# the harness a request naming none runs its child under
DEFAULT_HARNESS = 'bro'
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
HOLD_HELP = "the child's user-involvement level; omitted lets the child use its unattended default"
MANUAL_HELP = (
  'register a manual summon instead of spawning: the request id becomes the token '
  'a user-launched `ride along --summoned <token>` session answers'
)
HARNESS_HELP = (
  f"the harness the child runs under: '{DEFAULT_HARNESS}' (default — the target's own LLM "
  "process) or 'claude' (a one-shot managed Claude Code session)"
)
GRANT_HELP = (
  "add a credential (KIND or KIND+INSTANCE) or summonable bro (@BRO) to the child's scope "
  '(repeatable)'
)
REVOKE_HELP = (
  "remove a credential kind (KIND) or summonable bro (@BRO) from the child's scope (repeatable)"
)
SHARE_HELP = (
  'give the child read access to an artifact ref this session can itself read (repeatable)'
)
INTO_HELP = "base the child's workspace on this git ref instead of the summoner's workspace HEAD"
DETACH_HELP = 'print the request id and exit after sending; collect it with summon check'


def manual_launch_command(request_id: str, target: str) -> str:
  """the ride command that launches a manual summon's child session — what the
  summoner relays to the user along with the token (the request id)."""
  return f'ride along --summoned {request_id} {target}'


def encode_may_summon(targets: Collection[str]) -> str:
  """an effective summon allow-list as the `MAY_SUMMON_ENV` value: the names
  sorted and comma-joined, empty for a run that may summon nothing."""
  return ','.join(sorted(set(targets)))


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


def effective_may_summon() -> tuple[str, ...]:
  """the summon allow-list as a rendering fact (`#may_summon`): the published
  list, with an unpublished one collapsed to empty — a run whose launcher
  published no list should plan no delegation."""
  return may_summon() or ()


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
  if keys == {'session'} and isinstance(summoned_by['session'], str):
    return None
  if keys == {'target', 'trail_id'} and all(isinstance(summoned_by[key], str) for key in keys):
    return {'trail_id': summoned_by['trail_id']}
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
  return 'look for the run with `rewind list`'


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
    raise SummonError(f'no reply to broker {kind!r} read within {timeout:.0f}s') from None
  except ConnectionError as error:
    raise SummonError(f'broker channel closed during {kind!r} read: {error}') from None
  payload = result.payload
  if payload.get('outcome') != 'ok':
    raise SummonError(str(payload.get('error', payload)))
  value = payload.get('value')
  if not isinstance(value, dict):
    raise SummonError(f'broker {kind!r} read returned a malformed value: {value!r}')
  return value


def _query_quest(client: 'Client', request_id: str, *, wait_seconds: float = 0) -> dict[str, Any]:
  from bro.broker.dispatcher import QUERY

  args: dict[str, Any] = {'id': request_id}
  if wait_seconds > 0:
    args['wait'] = wait_seconds
  value = _read_value(
    client,
    QUERY,
    args,
    timeout=max(ACCEPT_TIMEOUT, wait_seconds + ACCEPT_TIMEOUT),
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


def _await_answer(
  client: 'Client',
  request: 'Message',
  *,
  timeout: float,
  on_started: Optional[Callable[[str], None]] = None,
  silence_timeout: Optional[float] = None,
) -> str:
  """Wait for a direct result, consulting the journal whenever the wire is silent."""
  from bro.broker.brotocol import Tag

  trail_id: Optional[str] = None

  def _interim(message: 'Message') -> None:
    nonlocal trail_id
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
      )
    except TimeoutError:
      quest = _query_quest(client, request.quest_id)
      answer = _summon_answer(quest)
      if answer is not None:
        return answer
      queried_trail = quest.get('trail_id')
      if isinstance(queried_trail, str) and queried_trail != trail_id:
        trail_id = queried_trail
        if on_started is not None:
          on_started(queried_trail)
      continue
    except ConnectionError as error:
      raise SummonError(f'broker channel closed awaiting the summon result: {error}') from None
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
  step_id: Optional[int] = None,
  index: Optional[int] = None,
  client: Optional['Client'] = None,
  silence_timeout: Optional[float] = None,
) -> str:
  """Send one summon and wait for its answer."""
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
  )
  with _connection(client) as connection:
    request = _send_summon(connection, payload)
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
  """A repeatable journal-backed check: pending, or the retained answer."""

  pending: bool
  answer: Optional[str] = None
  trail_id: Optional[str] = None


def check_summon(request_id: str) -> SummonStatus:
  """Read one summon quest without consuming its retained result."""
  with _open_client() as client:
    quest = _query_quest(client, request_id)
  answer = _summon_answer(quest)
  trail_id = quest.get('trail_id')
  return SummonStatus(
    pending=answer is None,
    answer=answer,
    trail_id=trail_id if isinstance(trail_id, str) else None,
  )


def wait_summon(
  request_id: str,
  *,
  timeout: Optional[float] = None,
  client: Optional['Client'] = None,
  wait_seconds: Optional[float] = None,
) -> str:
  """Long-poll a summon quest until its retained terminal result is available."""
  if timeout is not None and timeout <= 0:
    raise SummonError('query wait must be positive')
  interval = (
    wait_seconds
    if wait_seconds is not None
    else min(timeout if timeout is not None else READ_WAIT_SECONDS, READ_WAIT_SECONDS)
  )
  if interval <= 0:
    raise SummonError('query wait must be positive')
  with _connection(client) as connection:
    while True:
      quest = _query_quest(connection, request_id, wait_seconds=interval)
      answer = _summon_answer(quest)
      if answer is not None:
        return answer


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


def _event_line(event: dict[str, Any]) -> str:
  request_id = event.get('quest')
  transition = event.get('transition')
  if transition == 'trail':
    return f'summon trail {event.get("trail_id")} (request {request_id})'
  if transition == 'ended':
    reason = f':{event["reason"]}' if event.get('reason') is not None else ''
    return f'summon ended {event.get("outcome")}{reason} (request {request_id})'
  if transition == 'denied':
    return f'{event.get("reason")} (request {request_id})'
  return f'summon {transition} (request {request_id})'


def watch_summons(wait_seconds: float = READ_WAIT_SECONDS) -> Generator[str]:
  """Yield ordered summon journal transitions from the moment the watch is armed."""
  if wait_seconds <= 0:
    raise SummonError('events wait must be positive')
  from bro.broker.dispatcher import EVENTS

  with _open_client() as client:
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
        yield f'summon watch gap: {error}; re-armed at {head}'
        continue
      events = value.get('events')
      if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise SummonError('events read returned malformed records')
      for event in events:
        sequence = event.get('seq')
        if not isinstance(sequence, int) or isinstance(sequence, bool):
          raise SummonError('events read returned a malformed sequence')
        cursor = max(cursor, sequence)
        if event.get('kind') == SUMMON:
          yield _event_line(event)


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


def _relay(await_answer: Callable[[], str]) -> int:
  try:
    result = await_answer()
  except SummonError as e:
    log.error('%s', e)
    return 1
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


def _check(request_id: str, wait: bool, timeout: Optional[float]) -> int:
  if timeout is not None and not wait:
    log.error('--timeout only sets the long-poll interval for --wait')
    return 1
  if wait:
    return _relay(lambda: wait_summon(request_id, timeout=timeout))
  try:
    status = check_summon(request_id)
  except SummonError as error:
    log.error('%s', error)
    return 1
  if status.pending:
    log.info('summon still running; %s', _trails_hint(status.trail_id))
    return PENDING_EXIT_CODE
  assert status.answer is not None
  print(status.answer)
  return 0


def main(argv: list[str]) -> Optional[int]:
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
      description="stream this session's ordered summon journal transitions. "
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
    parser.add_argument('request_id', help='request id printed by the original summon')
    parser.add_argument(
      '--wait',
      action='store_true',
      help='block until the retained terminal result arrives; concurrent waits and '
      'later checks are safe because journal reads are non-destructive',
    )
    parser.add_argument(
      '--timeout',
      type=float,
      help=f'with --wait: maximum seconds per journal long-poll (default: {READ_WAIT_SECONDS:.0f})',
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
  parser.add_argument('--into', metavar='REF', help=INTO_HELP)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP)
  parser.add_argument('--harness', default=None, help=HARNESS_HELP)
  parser.add_argument(
    '--timeout',
    type=float,
    metavar='SECONDS',
    help=f'seconds before the host kills the child (default: {DEFAULT_TIMEOUT:.0f})',
  )
  parser.add_argument('--manual', action='store_true', help=MANUAL_HELP)
  parser.add_argument('--detach', action='store_true', help=DETACH_HELP)
  args = parser.parse(argv)
  try:
    canonicalize(args, selection_from_args(args))
  except LLMSelectionError as error:
    log.error('%s', error)
    return 1
  if args['manual']:
    launch_owned = {
      '--timeout': args['timeout'],
      '--hold': args['hold'],
      '--harness': args['harness'],
      '--llm': args['llm'],
    }
    passed = sorted(flag for flag, value in launch_owned.items() if value is not None)
    if len(passed) > 0:
      log.error("a manual summon's launch owns %s; drop the flag(s)", ', '.join(passed))
      return 1
    if args['share'] is not None:
      log.error("a manual summon's container is not launched by the host; drop --share")
      return 1
  os.environ.setdefault('BRO_SHELL_COMMAND', ' '.join(parser.reconstruct(args, prog=['summon'])))
  if args['detach']:
    try:
      if args['manual']:
        request_id = summon_manual(
          args['target'],
          args['prompt'],
          into=args['into'],
          grant=args['grant'],
          revoke=args['revoke'],
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
    manual=args['manual'],
  )
