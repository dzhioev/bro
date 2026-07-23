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
  FAST_HELP,
  GRANT_HELP,
  HOLD_HELP,
  IN_PLACE_HELP,
  INTO_HELP,
  NO_TRAILS_HELP,
  REVOKE_HELP,
  create_bro_for_run,
  maybe_containerize,
  run_llm_spec,
)
from bro.launch._trace_format import format_tool_call, oneline, truncate
from bro.launch.resume import RESUME_LATEST, HistoryMessage
from llm.llm import EFFORT_LEVELS
from llm.mcp import HOLDS, canonical_name
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
    self._emit(f'→ {format_tool_call(name, arguments)}')

  def on_tool_result(self, name: str, result: dict[str, Any] | str) -> None:
    self._emit(f'← {canonical_name(name)}')


async def call_text(
  bro: Bro,
  initial: Optional[str],
  observer: Optional[Observer] = None,
  read_line: Optional[Callable[[], str]] = None,
  now: Callable[[], datetime] = datetime.now,
  history: Optional[list[HistoryMessage]] = None,
  hold: str = 'guided',
) -> None:
  """text-mode REPL: `[HH:MM:SS] bro: <reply>` lines, plain `> ` prompt.

  used when stdin/stdout isn't a TTY or when `--text` is forced. read_line,
  now, and observer are injectable for tests. `history` (a resumed
  conversation's prior exchanges) renders before the banner, with a date line
  wherever the day changes; `initial` may be None on a resume — the REPL then
  just prompts.
  """
  from workspace.banner import render_banner

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
    reply = await bro.send(initial, observer=effective_observer, surface='call', hold=hold)
    emit(reply)
  while True:
    try:
      message = read()
    except EOFError:
      return
    if len(message) == 0:
      continue
    reply = await bro.send(message, observer=effective_observer, surface='call', hold=hold)
    emit(reply)


def _tty_supported() -> bool:
  return sys.stdin.isatty() and sys.stdout.isatty()


def chat_main(
  argv: list[str],
  *,
  program: list[str],
  implied_fast: bool = False,
  default_hold: str = 'guided',
) -> Optional[int]:
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
  parser.add_argument('--fast', action='store_true', help=FAST_HELP)
  parser.add_argument('--effort', choices=EFFORT_LEVELS, default=None, help=EFFORT_HELP)
  parser.add_argument('--in-place', action='store_true', help=IN_PLACE_HELP)
  parser.add_argument('--no-trails', dest='no_trails', action='store_true', help=NO_TRAILS_HELP)
  # --no-trails acts only on the container hop; --in-place has no hop to act on.
  parser.add_exclusive_groups(['in_place'], ['no_trails'])
  # a resume reads the recorded trail and records the continuation — both need
  # the trails sink --no-trails turns off.
  parser.add_exclusive_groups(['resume'], ['no_trails'])
  parser.add_argument('--grant', action='append', default=None, metavar='NAME', help=GRANT_HELP)
  parser.add_argument('--revoke', action='append', default=None, metavar='NAME', help=REVOKE_HELP)
  parser.add_argument('--into', metavar='REF', help=INTO_HELP)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP.format(default_hold))
  args = parser.parse(argv)
  os.environ.setdefault('PPP_SHELL_COMMAND', ' '.join(parser.reconstruct(args, prog=program)))

  if args['what'] is None and args['resume'] is None:
    log.error('what is required unless --resume is given')
    return 1
  if os.environ.get('CW_IN_CONTAINER') is not None and not args['in_place']:
    log.error(
      "bro chat refuses an implicit in-container run; pass --in-place to use this container's scope"
    )
    return 1

  # decide TUI-vs-text on the host, before the hop: `run_in_container` always
  # allocates a `-it` PTY, so an in-container `_tty_supported()` check would pick the
  # TUI even for a piped/redirected host invocation. force text mode into the
  # container whenever the host can't back the TUI (or the user asked for it).
  force_text = args['text'] or not _tty_supported()
  fast = args['fast'] or implied_fast
  inner_args = [args['what']] if args['what'] is not None else []
  if force_text:
    inner_args.append('--text')
  if args['resume'] is not None:
    inner_args.extend(['--resume', args['resume']])
  if fast:
    inner_args.append('--fast')
  if args['effort'] is not None:
    inner_args.extend(['--effort', args['effort']])
  # always explicit: the alias defaults diverge (call attends, bro chat guides),
  # so the inner `bro chat` must not fall back to its own default
  hold = args['hold'] if args['hold'] is not None else default_hold
  inner_args.extend(['--hold', hold])
  hopped = maybe_containerize(
    cli_name='bro-chat' if program == ['bro', 'chat'] else program[0],
    verb='chat',
    bro_name=args['bro'],
    inner_args=inner_args,
    in_place=args['in_place'],
    no_trails=args['no_trails'],
    grant=args['grant'],
    revoke=args['revoke'],
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
      spec = run_llm_spec(bro_class, fast=fast, effort=args['effort'])
    except NotImplementedError as e:
      # --effort on a provider without the knob — an explicit ask, so a clean
      # error instead of fast mode's silent fallback.
      log.error('%s', e)
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
        log.error('%s', e)
        return 1
    bro = resumed.bro
    history = resumed.history
    log.info('resumed trail %s (%d prior messages)', resumed.trail_id, len(history))
  else:
    log.verbose('creating bro %s', args['bro'])
    try:
      bro = create_bro_for_run(args['bro'], fast=fast, effort=args['effort'])
    except NotImplementedError as e:
      # --effort on a provider without the knob — an explicit ask, so a clean
      # error instead of fast mode's silent fallback.
      log.error('%s', e)
      return 1
  initial: Optional[str] = args['what']
  use_tui = not args['text'] and _tty_supported()

  try:
    if use_tui:
      from bro.launch.call_tui import ChatApp

      ChatApp(bro, initial, history=history, hold=hold).run()
    else:
      asyncio.run(call_text(bro, initial, history=history, hold=hold))
  except BroRaised as e:
    log.error('raised: %s', e.reason)
    return 1
  except KeyboardInterrupt:
    return 130
  finally:
    # the conversation survives as its trail — point the user at the pickup
    if bro.trail_id is not None:
      log.info(
        'conversation recorded as trail %s; continue it with: %s %s --resume %s',
        bro.trail_id,
        ' '.join(program),
        args['bro'],
        bro.trail_id,
      )


def main(argv: list[str]) -> Optional[int]:
  return chat_main(argv, program=['call'], implied_fast=True, default_hold='attended')
