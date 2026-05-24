import asyncio
import sys

import base.args
from bro.bro import Bro, BroRaised
from do.do import do

__cli_name__ = 'do-task'


async def do_task(bro: Bro, task_id: str) -> str:
  return await do(bro, f'fix the flow task {task_id}')


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='run a bro on a flow task')
  parser.add_argument('bro', help='bro name')
  parser.add_argument('task_id', help='flow task id')
  args = parser.parse(argv)

  from bro.registry import get_bro

  try:
    result = asyncio.run(do_task(get_bro(args['bro']), args['task_id']))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  print(result)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
