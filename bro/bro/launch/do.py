import asyncio
import sys

import base.args
from bro.bro import Bro

__cli_name__ = 'do'


async def do(bro: Bro, what: str) -> str:
  return await bro.run(what)


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='run a bro on the given input')
  parser.add_argument('bro', help='bro name')
  parser.add_argument('what', help='input to send to the bro')
  args = parser.parse(argv)

  from bro.registry import get_bro

  result = asyncio.run(do(get_bro(args['bro']), args['what']))
  print(result)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
