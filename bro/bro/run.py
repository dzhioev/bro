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
  run_parser.add_argument('input', help='input to send to the bro')

  sub.add_parser('list', help='list registered bros')

  show_parser = sub.add_parser('show', help='print an info card for a bro')
  show_parser.add_argument('name', help='bro name')
  show_parser.add_argument(
    '--system-prompt',
    action='store_true',
    help='also include the full assembled system prompt',
  )

  args = parser.parse(argv)

  from bro.bro import BroRaised
  from bro.registry import get_bro, list_bros

  command = args.get('command')
  if command == 'list':
    for b in list_bros():
      print(f'{b.name}: {b.description}')
    return

  if command == 'run':
    b = get_bro(args['name'])
    try:
      result = asyncio.run(b.run(args['input']))
    except BroRaised as e:
      print(f'raised: {e.reason}', file=sys.stderr)
      return 1
    print(result)
    return

  if command == 'show':
    from bro.show import format_card

    b = get_bro(args['name'])
    card = asyncio.run(format_card(b, include_system_prompt=args['system_prompt']))
    print(card, end='')
    return

  parser.print_help(sys.stderr)
  return 1


if __name__ == '__main__':
  sys.exit(main(sys.argv))
