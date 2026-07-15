import asyncio
import http.client
import os
import sys
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Optional, TextIO

import base.args
from base import log
from bro.bro import BroRaised
from bro.bros.bro import Bro
from bro.launch._cli import (
  EFFORT_HELP,
  GRANT_CRED_HELP,
  GRANT_SUMMON_HELP,
  IN_PLACE_HELP,
  INTO_HELP,
  NO_TRAILS_HELP,
  REVOKE_CRED_HELP,
  REVOKE_SUMMON_HELP,
  SLOW_HELP,
  create_bro_for_run,
  maybe_containerize,
  run_llm_spec,
)
from bro.launch._trace_format import compact_value, oneline, truncate
from bro.launch.resume import RESUME_LATEST, HistoryMessage
from llm.observer import Observer

__cli_name__ = 'call'

RESUME_HELP = (
  'continue a recorded call conversation instead of starting a fresh one: pass the trail id '
  "printed when that call ended, or omit the value to continue the bro's newest recorded call. "
  'prior exchanges are rendered as history and the continuation is recorded as a new trail'
)

# date-separator format shared with the TUI's DateSeparator
DATE_FORMAT = '%a, %b %-d, %Y'


_REASONING_LIMIT = 240
_MESSAGE_LIMIT = 240
_VALUE_LIMIT = 240


def _now_hms() -> str:
  return datetime.now().strftime('%H:%M:%S')


class TextRenderer(Observer):
  """render observed events as one-liners that share the `[HH:MM:SS] bro …` shape
  with text-mode reply emission. background activity and final replies read as
  one stream — no multi-line panels, no extra blank lines.
  """

  def __init__(
    self,
    prefix: str,
    file: Optional[TextIO] = None,
    now: Callable[[], str] = _now_hms,
  ):
    self._prefix = prefix
    self._file = file if file is not None else sys.stdout
    self._now = now

  def _emit(self, body: str) -> None:
    print(f'[{self._now()}] {self._prefix} {body}', file=self._file)
    self._file.flush()

  def on_reasoning(self, text: str) -> None:
    self._emit(f'· thinking: {truncate(oneline(text), _REASONING_LIMIT)}')

  def on_assistant_message(self, text: str, terminal: bool) -> None:
    # skip terminal — call_text prints the reply itself as `[timestamp] bro: <reply>`,
    # so emitting here would double-render.
    if terminal:
      return
    self._emit(f'· says: {truncate(oneline(text), _MESSAGE_LIMIT)}')

  def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
    self._emit(f'→ {name} {truncate(compact_value(arguments), _VALUE_LIMIT)}')

  def on_tool_result(self, name: str, result: dict[str, Any] | str) -> None:
    self._emit(f'← {name} {truncate(compact_value(result), _VALUE_LIMIT)}')


async def call_text(
  bro: Bro,
  initial: Optional[str],
  observer: Optional[Observer] = None,
  read_line: Optional[Callable[[], str]] = None,
  now: Callable[[], datetime] = datetime.now,
  history: Optional[list[HistoryMessage]] = None,
) -> None:
  """text-mode REPL: `[HH:MM:SS] bro: <reply>` lines, plain `> ` prompt.

  used when stdin/stdout isn't a TTY or when `--text` is forced. read_line,
  now, and observer are injectable for tests. `history` (a resumed
  conversation's prior exchanges) renders before the banner, with a date line
  wherever the day changes; `initial` may be None on a resume — the REPL then
  just prompts.
  """
  from cw import render_banner

  read = read_line if read_line is not None else (lambda: input('> '))
  effective_observer: Observer = observer if observer is not None else TextRenderer(prefix=bro.name)

  def emit(reply: str) -> None:
    timestamp = now().strftime('%H:%M:%S')
    print(f'[{timestamp}] {bro.name}: {reply}')

  history_messages = history if history is not None else []
  last_day: Optional[date] = None
  for message in history_messages:
    day = message.when.date()
    if day != last_day:
      print(f'--- {day.strftime(DATE_FORMAT)} ---')
      last_day = day
    speaker = 'you' if message.by_user else bro.name
    print(f'[{message.when.strftime("%H:%M:%S")}] {speaker}: {message.text}')
  if len(history_messages) > 0:
    # close the history block with today's date line, so the live exchanges
    # below read against the right day
    print(f'--- {now().date().strftime(DATE_FORMAT)} ---')

  # opening bro message: the cw banner (session environment facts), before the
  # first user message is sent. visual form — its ANSI renders in the terminal.
  # pass the bro name so the logo shows on an in-process (--in-place) run, whose
  # environment doesn't carry this bro's CW_BRO.
  print(f'[{now().strftime("%H:%M:%S")}] {bro.name}:')
  print(render_banner(llm=False, bro=bro.name))

  if initial is not None:
    reply = await bro.send(initial, observer=effective_observer, entry_point='call')
    emit(reply)
  while True:
    try:
      message = read()
    except EOFError:
      return
    if len(message) == 0:
      continue
    reply = await bro.send(message, observer=effective_observer, entry_point='call')
    emit(reply)


def _tty_supported() -> bool:
  return sys.stdin.isatty() and sys.stdout.isatty()


def chat_main(argv: list[str], *, program: list[str]) -> Optional[int]:
  from cw import EFFORT_LEVELS

  parser = base.args.Parser(
    prog=' '.join(program), description='open an interactive session with a bro'
  )
  parser.add_argument('bro', help='bro name')
  parser.add_argument(
    'what', nargs='?', help='first message to send to the bro (optional with --resume)'
  )
  parser.add_argument(
    '--text',
    action='store_true',
    help='force text mode (timestamped lines) instead of the Textual chat UI',
  )
  parser.add_argument(
    '--resume', nargs='?', const=RESUME_LATEST, default=None, metavar='TRAIL_ID', help=RESUME_HELP
  )
  parser.add_argument('--slow', action='store_true', help=SLOW_HELP)
  parser.add_argument('--effort', choices=EFFORT_LEVELS, default=None, help=EFFORT_HELP)
  parser.add_argument('--in-place', action='store_true', help=IN_PLACE_HELP)
  parser.add_argument('--no-trails', dest='no_trails', action='store_true', help=NO_TRAILS_HELP)
  # --no-trails acts only on the container hop; --in-place has no hop to act on.
  parser.add_exclusive_groups(['in_place'], ['no_trails'])
  # a resume reads the recorded trail and records the continuation — both need
  # the trails sink --no-trails turns off.
  parser.add_exclusive_groups(['resume'], ['no_trails'])
  parser.add_argument(
    '--grant-cred', action='append', default=None, metavar='SECRET', help=GRANT_CRED_HELP
  )
  parser.add_argument(
    '--revoke-cred', action='append', default=None, metavar='SECRET', help=REVOKE_CRED_HELP
  )
  parser.add_argument(
    '--grant-summon', action='append', default=None, metavar='BRO', help=GRANT_SUMMON_HELP
  )
  parser.add_argument(
    '--revoke-summon', action='append', default=None, metavar='BRO', help=REVOKE_SUMMON_HELP
  )
  parser.add_argument('--into', metavar='REF', help=INTO_HELP)
  args = parser.parse(argv)
  os.environ.setdefault('PPP_SHELL_COMMAND', ' '.join(parser.reconstruct(args, prog=program)))

  if args['what'] is None and args['resume'] is None:
    print('what is required unless --resume is given', file=sys.stderr)
    return 1
  if os.environ.get('CW_IN_CONTAINER') is not None and not args['in_place']:
    print(
      'bro chat refuses an implicit in-container run; pass --in-place to use this '
      "container's scope",
      file=sys.stderr,
    )
    return 1

  # decide TUI-vs-text on the host, before the hop: `run_in_container` always
  # allocates a `-it` PTY, so an in-container `_tty_supported()` check would pick the
  # TUI even for a piped/redirected host invocation. force text mode into the
  # container whenever the host can't back the TUI (or the user asked for it).
  force_text = args['text'] or not _tty_supported()
  inner_args = [args['what']] if args['what'] is not None else []
  if force_text:
    inner_args.append('--text')
  if args['resume'] is not None:
    inner_args.extend(['--resume', args['resume']])
  if args['slow']:
    inner_args.append('--slow')
  if args['effort'] is not None:
    inner_args.extend(['--effort', args['effort']])
  hopped = maybe_containerize(
    cli_name='bro-chat' if program == ['bro', 'chat'] else program[0],
    verb='chat',
    bro_name=args['bro'],
    inner_args=inner_args,
    in_place=args['in_place'],
    no_trails=args['no_trails'],
    grant_cred=args['grant_cred'],
    revoke_cred=args['revoke_cred'],
    grant_summon=args['grant_summon'],
    revoke_summon=args['revoke_summon'],
    into=args['into'],
  )
  if hopped is not None:
    return hopped

  history: Optional[list[HistoryMessage]] = None
  if args['resume'] is not None:
    from bro.launch.resume import resume
    from bro.registry import get_class
    from trails.client import default_client

    bro_class = get_class(args['bro'])
    try:
      spec = run_llm_spec(bro_class, fast=not args['slow'], effort=args['effort'])
    except NotImplementedError as e:
      # --effort on a provider without the knob — an explicit ask, so a clean
      # error instead of fast mode's silent fallback.
      print(str(e), file=sys.stderr)
      return 1
    with default_client() as client:
      try:
        # the continuation runs the class's current spec (as a fresh call
        # would), not the spec recorded on the trail
        resumed = resume(
          client,
          args['bro'],
          args['resume'],
          llm_spec=spec if spec is not None else bro_class.llm_spec,
        )
      except (ValueError, http.client.HTTPException) as e:
        print(str(e), file=sys.stderr)
        return 1
    bro = resumed.bro
    history = resumed.history
    log.info('resumed trail %s (%d prior messages)', resumed.trail_id, len(history))
  else:
    try:
      bro = create_bro_for_run(args['bro'], fast=not args['slow'], effort=args['effort'])
    except NotImplementedError as e:
      # --effort on a provider without the knob — an explicit ask, so a clean
      # error instead of fast mode's silent fallback.
      print(str(e), file=sys.stderr)
      return 1
  initial: Optional[str] = args['what']
  use_tui = not args['text'] and _tty_supported()

  try:
    if use_tui:
      from bro.launch.call_tui import ChatApp

      ChatApp(bro, initial, history=history).run()
    else:
      asyncio.run(call_text(bro, initial, history=history))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  except KeyboardInterrupt:
    return 130
  finally:
    # the conversation survives as its trail — point the user at the pickup
    if bro.trail_id is not None:
      print(
        f'conversation recorded as trail {bro.trail_id}; continue it with: '
        f'{" ".join(program)} {args["bro"]} --resume {bro.trail_id}',
        file=sys.stderr,
      )


def main(argv: list[str]) -> Optional[int]:
  return chat_main(argv, program=['call'])
