import asyncio
import contextlib
import http.client
import importlib.util
import json
import os
import signal
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Optional, TextIO

import bro.base.args as base_args
from bro.base import log
from bro.bro import RAISE_EXIT_STATUS, AnswerDelivered, BroRaised
from bro.launch.llm_flags import (
  EFFORT_HELP,
  FAST_HELP,
  add_llm_flags,
  canonicalize,
  selection_from_args,
)
from bro.launch.resume import RESUME_LATEST
from bro.launch.run import HOLD_HELP, create_bro_for_run, run_llm_spec
from bro.llm.observer import Observer
from bro.llm.providers import LLMSelectionError
from bro.mcp import HOLDS
from bro.native.runner import Runner
from bro.run_lifecycle import RunLifecycle
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

FORK_HELP = (
  'fork a recorded conversation into a new trail: pass any trail id, or omit the value to fork '
  "the bro's newest recorded call. prior exchanges are rendered as history and the fork uses "
  "the bro class's current LLM recipe"
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


async def _turn(runner: Runner, message: str, *, observer: Observer, hold: str) -> Optional[str]:
  task = asyncio.create_task(runner.send(message, observer=observer, surface='call', hold=hold))
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
  runner: Runner,
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
  configuration = preset(preset_name, context_label=runner.bro.name)
  interruption_number = 0

  with DisplaySession(configuration, renderer) as session:
    if history is not None:
      session.consume(history)
    session.consume(
      _surface_notice(
        'surface:banner',
        render_banner(llm=False, bro=runner.bro.name),
        now(),
        trusted_visual=True,
      )
    )
    observer = LiveDisplayObserver(session, now=lambda: now().astimezone())

    async def exchange(message: str) -> None:
      nonlocal interruption_number
      reply = await _turn(runner, message, observer=observer, hold=hold)
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


def chat_main(argv: list[str], *, program: list[str]) -> Optional[int]:
  parser = base_args.Parser(
    prog=' '.join(program), description='open an interactive session with a bro'
  )
  parser.add_argument('bro', help='bro name')
  parser.add_argument('what', nargs='?', help='first message to send to the bro')
  parser.add_argument(
    '--fork', nargs='?', const=RESUME_LATEST, default=None, metavar='TRAIL_ID', help=FORK_HELP
  )
  parser.add_argument('--continue-trail', default=None, help=base_args.SUPPRESS)
  parser.add_argument('--continue-llm', default=None, help=base_args.SUPPRESS)
  parser.add_argument(
    '--at',
    type=int,
    default=None,
    metavar='STEP_ID',
    help='with --fork: fork the conversation at this step instead of the latest consistent point',
  )
  add_llm_flags(parser, effort_help=EFFORT_HELP, fast_help=FAST_HELP)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP.format('guided'))
  args = parser.parse(argv)
  try:
    selection = selection_from_args(args)
    canonicalize(args, selection)
  except LLMSelectionError as error:
    log.error('%s', error)
    return 1
  os.environ.setdefault('BRO_SHELL_COMMAND', ' '.join(parser.reconstruct(args, prog=program)))

  continuing = args['continue_trail'] is not None or args['continue_llm'] is not None
  if (args['continue_trail'] is None) != (args['continue_llm'] is None):
    log.error('--continue-trail and --continue-llm must be passed together')
    return 1
  if continuing and (args['fork'] is not None or args['what'] is not None):
    log.error('--continue-trail cannot be combined with --fork or an initial message')
    return 1
  if args['at'] is not None and args['fork'] is None:
    log.error('--at names a fork point; it requires --fork')
    return 1

  try:
    run_spec = None if continuing else run_llm_spec(args['bro'], selection)
  except (KeyError, NotImplementedError, LLMSelectionError) as error:
    log.error('%s', error)
    return 1
  hold = args['hold'] if args['hold'] is not None else 'guided'

  from bro.launch.broxy import session_broxy

  with session_broxy():
    history: Optional[list[DisplayRecord]] = None
    if args['fork'] is not None or continuing:
      from bro.launch.resume import resume
      from bro.llm.llm import LLMSpec, NativeLLMSpec
      from bro.registry import get_class
      from bro.trails.store import default_store

      trail_ref = args['continue_trail'] if continuing else args['fork']
      assert trail_ref is not None
      if continuing:
        continued_llm = args['continue_llm']
        assert isinstance(continued_llm, str)
        try:
          continued_spec = LLMSpec.from_dict(json.loads(continued_llm))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
          log.error('invalid recorded LLM recipe: %s', error)
          return 1
        if not isinstance(continued_spec, NativeLLMSpec):
          log.error('recorded recipe %s is not runnable by the bro harness', continued_spec.TYPE)
          return 1
        fork_spec = continued_spec
      else:
        fork_spec = run_spec if run_spec is not None else get_class(args['bro']).llm_spec
      with default_store() as client:
        try:
          fork_arguments = {'llm_spec': fork_spec, 'at': args['at']}
          if continuing:
            fork_arguments['hold'] = hold
          forked = resume(client, args['bro'], trail_ref, **fork_arguments)
        except (ValueError, http.client.HTTPException) as error:
          log.error('%s', error)
          return 1
      runner = forked.runner
      history = forked.history
      log.info('forked trail %s (%d prior display records)', forked.trail_id, len(history))
    else:
      log.verbose('creating bro %s', args['bro'])
      runner = Runner(create_bro_for_run(args['bro'], selection))
    initial: Optional[str] = args['what']
    use_tui = _tui_supported()

    delivered: Optional[AnswerDelivered] = None
    try:
      with runner:
        if use_tui:
          from bro.launch.call_tui import ChatApp

          app = ChatApp(
            runner,
            initial,
            history=history,
            hold=hold,
            preset_name=PresetName.CHAT,
          )
          app.run()
          delivered = app.delivered
        else:
          asyncio.run(
            call_text(
              runner,
              initial,
              history=history,
              hold=hold,
              preset_name=PresetName.CHAT,
            )
          )
    except AnswerDelivered as escaped:
      delivered = escaped
    except BroRaised as error:
      log.error('raised: %s', error.reason)
      return RAISE_EXIT_STATUS
    except KeyboardInterrupt:
      return 130
    finally:
      if runner.trail_id is not None:
        log.info(
          'conversation recorded as trail %s; fork it with: %s %s --fork %s',
          runner.trail_id,
          ' '.join(program),
          args['bro'],
          runner.trail_id,
        )
    if delivered is not None:
      return _relay_summoned_answer(delivered.answer)
    return None


def _relay_summoned_answer(answer: str) -> int:
  """send a summoned conversation's `answer`-tool result to the summoner as the
  run terminal — the chat surface's half of the bare `answer` flavor."""
  channel = RunLifecycle.from_env()
  if channel is None:
    log.error('no broker channel; the answer cannot reach the summoner: %s', answer)
    return 1
  channel.completed(answer, 'ok')
  channel.close()
  log.info('answer delivered to the summoner')
  return 0
