import asyncio
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, Optional, TextIO

import base.args
from bro.bro import BroRaised
from bro.bros.bro import Bro
from do._cli import (
  GRANT_HELP,
  NO_CONTAINER_HELP,
  NO_TRAILS_HELP,
  REVOKE_HELP,
  SLOW_HELP,
  create_bro_for_run,
  maybe_containerize,
)
from do._trace_format import compact_value, oneline, truncate
from llm.observer import Observer

__cli_name__ = 'call'


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
    # skip terminal — call_text prints the reply itself as `[ts] bro: <reply>`,
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
  initial: str,
  observer: Optional[Observer] = None,
  read_line: Optional[Callable[[], str]] = None,
  now: Callable[[], datetime] = datetime.now,
) -> None:
  """text-mode REPL: `[HH:MM:SS] bro: <reply>` lines, plain `> ` prompt.

  used when stdin/stdout isn't a TTY or when `--text` is forced. read_line,
  now, and observer are injectable for tests.
  """
  from cw import render_banner

  read = read_line if read_line is not None else (lambda: input('> '))
  effective_observer: Observer = observer if observer is not None else TextRenderer(prefix=bro.name)

  def emit(reply: str) -> None:
    ts = now().strftime('%H:%M:%S')
    print(f'[{ts}] {bro.name}: {reply}')

  # opening bro message: the cw banner (session environment facts), before the
  # first user message is sent. visual form — its ANSI renders in the terminal.
  # pass the bro name so the logo shows even though a `call` container doesn't
  # forward CW_BRO.
  print(f'[{now().strftime("%H:%M:%S")}] {bro.name}:')
  print(render_banner(llm=False, bro=bro.name))

  reply = await bro.send(initial, observer=effective_observer)
  emit(reply)
  while True:
    try:
      message = read()
    except EOFError:
      return
    if len(message) == 0:
      continue
    reply = await bro.send(message)
    emit(reply)


def _tty_supported() -> bool:
  return sys.stdin.isatty() and sys.stdout.isatty()


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='open an interactive session with a bro')
  parser.add_argument('bro', help='bro name')
  parser.add_argument('what', help='first message to send to the bro')
  parser.add_argument(
    '--text',
    action='store_true',
    help='force text mode (timestamped lines) instead of the Textual chat UI',
  )
  parser.add_argument('--slow', action='store_true', help=SLOW_HELP)
  parser.add_argument(
    '--no-container', dest='no_container', action='store_true', help=NO_CONTAINER_HELP
  )
  parser.add_argument('--no-trails', dest='no_trails', action='store_true', help=NO_TRAILS_HELP)
  # --no-trails acts only on the container hop; --no-container has no hop to act on.
  parser.add_exclusive_groups(['no_container'], ['no_trails'])
  parser.add_argument('--grant', action='append', default=None, metavar='SECRET', help=GRANT_HELP)
  parser.add_argument('--revoke', action='append', default=None, metavar='SECRET', help=REVOKE_HELP)
  args = parser.parse(argv)

  # decide TUI-vs-text on the host, before the hop: `run_in_container` always
  # allocates a `-it` PTY, so an in-container `_tty_supported()` check would pick the
  # TUI even for a piped/redirected host invocation. force text mode into the
  # container whenever the host can't back the TUI (or the user asked for it).
  force_text = args['text'] or not _tty_supported()
  inner_args = [args['what']]
  if force_text:
    inner_args.append('--text')
  if args['slow']:
    inner_args.append('--slow')
  hopped = maybe_containerize(
    cli_name='call',
    bro_name=args['bro'],
    inner_args=inner_args,
    no_container=args['no_container'],
    no_trails=args['no_trails'],
    grant=args['grant'],
    revoke=args['revoke'],
  )
  if hopped is not None:
    return hopped

  bro = create_bro_for_run(args['bro'], fast=not args['slow'])
  use_tui = not args['text'] and _tty_supported()

  try:
    if use_tui:
      from do.call_tui import ChatApp

      ChatApp(bro, args['what']).run()
    else:
      asyncio.run(call_text(bro, args['what']))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  except KeyboardInterrupt:
    return 130
