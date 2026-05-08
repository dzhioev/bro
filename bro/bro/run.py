import asyncio
import sys

import base.args
from base import log

__cli_name__ = 'bro'


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='run a bro agent')
  sub = parser.add_subparsers(dest='command')

  run_parser = sub.add_parser('run', help='run a bro on a single input')
  run_parser.add_argument('name', help='bro name')
  run_parser.add_argument('--input', '-i', required=True, help='input text')

  sub.add_parser('list', help='list registered bros')

  args = parser.parse(argv)

  import bro.bros  # noqa: F401 — triggers registration

  from bro.registry import get_bro, list_bros

  command = args.get('command')
  if command == 'list':
    for b in list_bros():
      print(f'{b.name}: {b.description}')
    return

  if command == 'run':
    b = get_bro(args['name'])
    result = asyncio.run(b.run(args['input']))
    print(result)
    return

  parser.print_help(sys.stderr)
  return 1


if __name__ == '__main__':
  sys.exit(main(sys.argv))
