import asyncio
import contextlib
import http.client
import importlib.util
import os
import signal
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Optional, TextIO

import bro.base.args as base_args
from bro.base import log
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
from bro.launch.resume import RESUME_LATEST
from bro.llm.llm import EFFORT_LEVELS
from bro.llm.mcp import HOLDS
from bro.llm.observer import Observer
from bro.trails.display import (
  DisplayRecord,
  DisplaySession,
  LiveDisplayObserver,
  Notice,
  Origin,
  PresetName,
  StreamRenderer,
  preset,
)

__cli_name__ = 'call'

RESUME_HELP = (
  'continue a recorded call conversation instead of starting a fresh one: pass the trail id '
  "printed when that call ended, or omit the value to continue the bro's newest recorded call. "
  'prior exchanges are rendered as history and the continuation is recorded as a new trail'
)

INTERRUPTED_NOTICE = '⨯ interrupted'


@contextlib.contextmanager
def _interruptible(task: asyncio.Task) -> Iterator[None]:
  """Route SIGINT to cancellation for the duration of one running turn."""
  loop = asyncio.get_running_loop()
  loop.add_signal_handler(signal.SIGINT, task.cancel)
  with contextlib.ExitStack() as stack:
    stack.callback(loop.remove_signal_handler, signal.SIGINT)
    yield


async def _turn(bro: Bro, message: str, *, observer: Observer, hold: str) -> Optional[str]:
  task = asyncio.create_task(bro.send(message, observer=observer, surface='call', hold=hold))
  with _interruptible(task):
    try:
      return await task
    except asyncio.CancelledError:
      return None


def _surface_notice(
  key: str,
  content: str,
  when: datetime,
  *,
  level: str = 'info',
  trusted_visual: bool = False,
) -> Notice:
  return Notice(
    key=key,
    origin=Origin.SURFACE,
    timestamp=when.astimezone().isoformat(),
    content=content,
    level=level,
    trusted_visual=trusted_visual,
  )


async def call_text(
  bro: Bro,
  initial: Optional[str],
  read_line: Optional[Callable[[], str]] = None,
  now: Callable[[], datetime] = datetime.now,
  history: Optional[list[DisplayRecord]] = None,
  hold: str = 'guided',
  preset_name: PresetName = PresetName.CALL,
  output: Optional[TextIO] = None,
) -> None:
  """Run the conversation REPL through the stream trails frontend."""
  from bro.workspace.banner import render_banner

  read = read_line if read_line is not None else (lambda: input('> '))
  destination = output if output is not None else sys.stdout
  renderer = StreamRenderer(destination)
  configuration = preset(preset_name, context_label=bro.name)
  interruption_number = 0

  with DisplaySession(configuration, renderer) as session:
    if history is not None:
      session.consume(history)
    session.consume(
      _surface_notice(
        'surface:banner',
        render_banner(llm=False, bro=bro.name),
        now(),
        trusted_visual=True,
      )
    )
    observer = LiveDisplayObserver(session, now=lambda: now().astimezone())

    async def exchange(message: str) -> None:
      nonlocal interruption_number
      reply = await _turn(bro, message, observer=observer, hold=hold)
      if reply is None:
        session.consume(
          _surface_notice(
            f'surface:interruption:{interruption_number}',
            INTERRUPTED_NOTICE,
            now(),
            level='interruption',
          )
        )
        interruption_number += 1

    if initial is not None:
      await exchange(initial)
    while True:
      try:
        message = read()
      except EOFError:
        return
      if len(message) == 0:
        continue
      await exchange(message)


def _tty_supported() -> bool:
  return sys.stdin.isatty() and sys.stdout.isatty()


def _tui_supported() -> bool:
  return (
    _tty_supported()
    and importlib.util.find_spec('rich') is not None
    and importlib.util.find_spec('textual') is not None
  )


def chat_main(
  argv: list[str],
  *,
  program: list[str],
  implied_fast: bool = False,
  default_hold: str = 'guided',
) -> Optional[int]:
  parser = base_args.Parser(
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
  parser.add_argument(
    '--at',
    type=int,
    default=None,
    metavar='STEP_ID',
    help='with --resume: fork the conversation at this step of the resumed trail '
    'instead of its latest consistent point',
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
  os.environ.setdefault('BRO_SHELL_COMMAND', ' '.join(parser.reconstruct(args, prog=program)))

  if args['what'] is None and args['resume'] is None:
    log.error('what is required unless --resume is given')
    return 1
  if args['at'] is not None and args['resume'] is None:
    log.error('--at names a fork point of a resumed trail; it requires --resume')
    return 1
  if os.environ.get('CW_IN_CONTAINER') is not None and not args['in_place']:
    log.error(
      "bro chat refuses an implicit in-container run; pass --in-place to use this container's scope"
    )
    return 1

  # decide TUI-vs-text on the host, before the hop: `run_in_container` always
  # allocates a `-it` PTY, so capability selection runs before the hop and the
  # selected stream fallback is forwarded into the container.
  force_text = args['text'] or not _tui_supported()
  fast = args['fast'] or implied_fast
  inner_args = [args['what']] if args['what'] is not None else []
  if force_text:
    inner_args.append('--text')
  if args['resume'] is not None:
    inner_args.extend(['--resume', args['resume']])
  if args['at'] is not None:
    inner_args.extend(['--at', str(args['at'])])
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

  history: Optional[list[DisplayRecord]] = None
  if args['resume'] is not None:
    from bro.launch.resume import resume
    from bro.registry import get_class
    from bro.trails.client import default_client

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
          at=args['at'],
        )
      except (ValueError, http.client.HTTPException) as e:
        log.error('%s', e)
        return 1
    bro = resumed.bro
    history = resumed.history
    log.info('resumed trail %s (%d prior display records)', resumed.trail_id, len(history))
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
  use_tui = not args['text'] and _tui_supported()
  preset_name = PresetName.CHAT if program == ['bro', 'chat'] else PresetName.CALL

  try:
    with bro:
      if use_tui:
        from bro.launch.call_tui import ChatApp

        ChatApp(
          bro,
          initial,
          history=history,
          hold=hold,
          preset_name=preset_name,
        ).run()
      else:
        asyncio.run(
          call_text(
            bro,
            initial,
            history=history,
            hold=hold,
            preset_name=preset_name,
          )
        )
  except BroRaised as error:
    log.error('raised: %s', error.reason)
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
