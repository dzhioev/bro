"""summon — request another bro through the session broker.

The module owns the summon request shape, the facts a summoned run reads off
its environment, and the summoning surfaces: blocking, detached, and manual.
Everything done to the quest a summon opens — reading its outcome or
conversation, talking on it, cancelling it — is `bro.quest`.

A detached or manual request returns its quest id only after the first
correlated message is the host's ``accepted`` mark; an immediate result is
interpreted as the refusal or launch failure it carries.
A blocking wait bounds silence rather than the run: after silence it reads the
quest's journal record, returning a retained answer or an open child question,
and otherwise resumes waiting while the host-owned Worker deadline keeps the
quest live.

Unlike the substrate CLI, an unset ``BROKER_CHANNEL`` is an error.
Broker imports stay deferred so importing summon constants does not pull in the
broker implementation on pre-gate launch paths.
"""

import json
import os
import shlex
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, Optional

import bro.base.args as base_args
from bro import quest
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
from bro.quest import SUMMON, QuestError

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.client import Client

__cli_name__ = 'summon'

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
HOLD_HELP = "the child's user-involvement level; omitted lets the child use its unattended default"
MANUAL_HELP = (
  'register a manual summon instead of spawning: the quest id becomes the token '
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
DETACH_HELP = 'print the quest id and exit after host acceptance; read it with `quest check`'
TALK_HELP = (
  'widen the child quest chat rights from worker.say; comma-separated values from '
  'owner.say, owner.question, worker.say, worker.question'
)


def manual_launch_command(quest_id: str, target: str) -> str:
  """the ride command that launches a manual summon's child session — what the
  summoner relays to the user along with the token (the quest id)."""
  runtime = os.environ.get(RUNTIME_ENV)
  if runtime is None:
    raise RuntimeError(f'{RUNTIME_ENV} is missing from the managed session environment')
  executable = shlex.quote(f'{runtime}/venv/bin/ride')
  return f'{executable} along --summoned {quest_id} {target}'


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
  from bro.broker.brotocol import decode_talk
  from bro.broker.environment import BROKER_TALK

  raw = os.environ.get(BROKER_TALK)
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
    raise QuestError('prompt too large; share an artifact instead') from None


def _await_answer(
  client: 'Client',
  request: 'Message',
  *,
  timeout: float,
  on_started: Optional[Callable[[str], None]] = None,
  silence_timeout: Optional[float] = None,
) -> str | quest.Question:
  """Wait for a result or child question, consulting the journal on wire silence."""
  from bro.broker.brotocol import Tag

  trail_id: Optional[str] = None

  def _interim(message: 'Message') -> None:
    nonlocal trail_id
    if message.type == Tag.MESSAGE:
      log.info('summon says %s', quest._single_line(quest._text_from_payload(message.payload)))
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
      record = quest.query_quest(client, request.request_id)
      answer = quest.answer_of(record)
      if answer is not None:
        return answer
      questions = quest.open_questions(record, awaiting='owner')
      if len(questions) > 0:
        return questions[0]
      queried_trail = record.get('trail_id')
      if isinstance(queried_trail, str) and queried_trail != trail_id:
        trail_id = queried_trail
        if on_started is not None:
          on_started(queried_trail)
      continue
    except ConnectionError as error:
      raise QuestError(f'broker channel closed awaiting the summon result: {error}') from None
    if result.type == Tag.MESSAGE:
      return quest.question_from_message(result)
    return quest.interpret_result(result.payload, trail_id)


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
) -> str | quest.Question:
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
  with quest.connection(client) as connection:
    request = _send_summon(connection, payload)
    if on_sent is not None:
      on_sent(request.request_id)
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
    first = client.await_any(request, quest.ACCEPT_TIMEOUT)
  except TimeoutError:
    raise QuestError(
      f'no acceptance within {quest.ACCEPT_TIMEOUT:.0f}s of the summon request'
    ) from None
  except ConnectionError as error:
    raise QuestError(f'broker channel closed awaiting summon acceptance: {error}') from None
  if first.type == Tag.RESULT:
    quest.interpret_result(first.payload, None)
    raise QuestError(f'summon ended before acceptance: {first.payload}')
  if first.type != Tag.MARK or first.payload.get('transition') != 'accepted':
    raise QuestError(f'unexpected first summon reply: {first.payload}')


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
  with quest.open_client() as client:
    request = _send_summon(client, payload)
    _await_acceptance(client, request)
    return request.request_id


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
  with quest.open_client() as client:
    request = _send_summon(client, payload)
    _await_acceptance(client, request)
    return request.request_id


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
  """send one summon and relay its outcome as a CLI would: the quest id and
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
    client = quest.open_client()
  except QuestError as e:
    log.error('%s', e)
    return 1
  with client:
    try:
      request = _send_summon(client, payload)
    except QuestError as error:
      log.error('%s', error)
      return 1
    log.info('summon quest %s', request.request_id)
    if manual:
      try:
        _await_acceptance(client, request)
      except QuestError as e:
        log.error('%s', e)
        return 1
      log.info('have the user run: %s', manual_launch_command(request.request_id, target))
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


def _relay(await_answer: Callable[[], str | quest.Question]) -> int:
  try:
    result = await_answer()
  except QuestError as error:
    log.error('%s', error)
    return 1
  if isinstance(result, quest.Question):
    print(result.text)
    log.info(
      "quest question %s; reply with `quest say %s '<text>' --reply-to %s`",
      result.id,
      result.quest_id,
      result.id,
    )
    return quest.QUESTION_EXIT_CODE
  print(result)
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog='summon',
    description='summon a bro over the session channel; the quest it opens is read, '
    'talked to, and ended with `quest`',
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
        quest_id = summon_manual(
          args['target'],
          args['prompt'],
          into=args['into'],
          grant=args['grant'],
          revoke=args['revoke'],
          talk=args['talk'],
        )
        log.info('have the user run: %s', manual_launch_command(quest_id, args['target']))
      else:
        quest_id = summon_detached(
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
    except QuestError as error:
      log.error('%s', error)
      return 1
    print(quest_id)
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
