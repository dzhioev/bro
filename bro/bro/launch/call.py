import asyncio
import sys
from datetime import datetime
from typing import Callable

import base.args
from bro.bro import BaseBro, BroRaised
from llm.tracer import NullTracer, Tracer

__cli_name__ = 'call'


async def call_raw(
  bro: BaseBro,
  initial: str,
  tracer: Tracer | None = None,
  read_line: Callable[[], str] | None = None,
  now: Callable[[], datetime] = datetime.now,
) -> None:
  """raw-mode REPL: `[HH:MM:SS] bro: <reply>` lines, plain `> ` prompt.

  used when stdin/stdout isn't a TTY or when --raw is forced. read_line and
  now are injectable for tests.
  """
  read = read_line if read_line is not None else (lambda: input('> '))
  effective_tracer: Tracer = tracer if tracer is not None else NullTracer()

  def emit(reply: str) -> None:
    ts = now().strftime('%H:%M:%S')
    print(f'[{ts}] {bro.name}: {reply}')

  reply = await bro.send(initial, tracer=effective_tracer)
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
    '--raw',
    action='store_true',
    help='force raw text mode (timestamped lines) instead of the Textual chat UI',
  )
  args = parser.parse(argv)

  from bro.registry import get_bro

  bro = get_bro(args['bro'])
  use_tui = not args['raw'] and _tty_supported()

  try:
    if use_tui:
      from do.call_tui import ChatApp

      ChatApp(bro, args['what']).run()
    else:
      asyncio.run(call_raw(bro, args['what']))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  except KeyboardInterrupt:
    return 130


if __name__ == '__main__':
  sys.exit(main(sys.argv))
