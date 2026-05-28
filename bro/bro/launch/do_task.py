import asyncio
import sys

import base.args
from bro.bro import BaseBro, BroRaised
from do.do import do
from llm.tracer import Tracer

__cli_name__ = 'do-task'


async def do_task(bro: BaseBro, task: str, tracer: Tracer | None = None) -> str:
  # the task argument is opaque to us — id, dashed UUID, Notion URL, or free text
  # describing the task. the bro's system prompt is responsible for normalising it
  return await do(bro, f'fix the flow task: {task}', tracer=tracer)


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='run a bro on a flow task')
  parser.add_argument('bro', help='bro name')
  parser.add_argument(
    'task', help='flow task reference: id, dashed UUID, Notion URL, or description'
  )
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
    result = asyncio.run(do_task(bro, args['task'], tracer=tracer))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  print(result)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
