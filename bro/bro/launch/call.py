import asyncio
import sys
from datetime import datetime
from typing import Any, Callable, TextIO

import base.args
from bro.bro import BroRaised
from bro.bros.bro import Bro
from do._cli import FAST_HELP, NO_CONTAINER_HELP, create_bro_for_run, maybe_containerize
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
    file: TextIO | None = None,
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
  observer: Observer | None = None,
  read_line: Callable[[], str] | None = None,
  now: Callable[[], datetime] = datetime.now,
) -> None:
  """text-mode REPL: `[HH:MM:SS] bro: <reply>` lines, plain `> ` prompt.

  used when stdin/stdout isn't a TTY or when `--text` is forced. read_line,
  now, and observer are injectable for tests.
  """
  read = read_line if read_line is not None else (lambda: input('> '))
  effective_observer: Observer = observer if observer is not None else TextRenderer(prefix=bro.name)

  def emit(reply: str) -> None:
    ts = now().strftime('%H:%M:%S')
    print(f'[{ts}] {bro.name}: {reply}')

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


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='open an interactive session with a bro')
  parser.add_argument('bro', help='bro name')
  parser.add_argument('what', help='first message to send to the bro')
  parser.add_argument(
    '--text',
    action='store_true',
    help='force text mode (timestamped lines) instead of the Textual chat UI',
  )
  parser.add_argument('--fast', action='store_true', help=FAST_HELP)
  parser.add_argument(
    '--no-container', dest='no_container', action='store_true', help=NO_CONTAINER_HELP
  )
  args = parser.parse(argv)

  # decide TUI-vs-text on the host, before the hop: `run_in_container` always
  # allocates a `-it` PTY, so an in-container `_tty_supported()` check would pick the
  # TUI even for a piped/redirected host invocation. force text mode into the
  # container whenever the host can't back the TUI (or the user asked for it).
  force_text = args['text'] or not _tty_supported()
  inner_args = [args['what']]
  if force_text:
    inner_args.append('--text')
  if args['fast']:
    inner_args.append('--fast')
  hopped = maybe_containerize(
    cli_name='call',
    bro_name=args['bro'],
    inner_args=inner_args,
    no_container=args['no_container'],
  )
  if hopped is not None:
    return hopped

  try:
    bro = create_bro_for_run(args['bro'], fast=args['fast'])
  except NotImplementedError as e:
    print(f'--fast: {e}', file=sys.stderr)
    return 1
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


if __name__ == '__main__':
  sys.exit(main(sys.argv))
