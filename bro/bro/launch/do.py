import asyncio
import sys

import base.args
from bro.bro import Bro, BroRaised
from llm.tracer import Tracer

__cli_name__ = 'ask'


async def do(bro: Bro, what: str, tracer: Tracer | None = None) -> str:
  return await bro.run(what, tracer=tracer)


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='run a bro on the given input')
  parser.add_argument('bro', help='bro name')
  parser.add_argument('what', help='input to send to the bro')
  parser.add_argument(
    '--rich',
    action='store_true',
    help='render the trace as colored rich panels instead of plain log lines',
  )
  args = parser.parse(argv)

  from bro.registry import get_bro

  bro = get_bro(args['bro'])
  tracer: Tracer | None = None
  if args['rich']:
    from llm.tracer import RichConsoleTracer

    tracer = RichConsoleTracer(prefix=bro.name)
  try:
    result = asyncio.run(do(bro, args['what'], tracer=tracer))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  print(result)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
