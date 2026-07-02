import asyncio
import sys
from typing import Optional

import base.args

__cli_name__ = 'bro'


def _command_list() -> None:
  from bro.registry import list_classes

  for cls in list_classes():
    print(f'{cls.name}: {cls.description}')


def _command_run(name: str, input: str, rich: bool) -> Optional[int]:
  from bro.bro import BroRaised
  from bro.registry import create_bro

  b = create_bro(name)
  observer = None
  if rich:
    from llm.observer import RichConsoleRenderer

    observer = RichConsoleRenderer(prefix=b.name)
  try:
    result = asyncio.run(b.run(input, observer=observer))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  print(result)


def _command_show(name: str, system_prompt: bool) -> None:
  from bro.registry import create_bro
  from bro.show import format_card

  b = create_bro(name)
  card = asyncio.run(format_card(b, include_system_prompt=system_prompt))
  print(card, end='')


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='run a bro agent')
  subparser = parser.add_subparsers(dest='command')

  run_parser = subparser.add_parser('run', help='run a bro on a single input')
  run_parser.add_argument('name', help='bro name')
  run_parser.add_argument('input', help='input to send to the bro')
  run_parser.add_argument(
    '--rich',
    action='store_true',
    help='render the trace as colored rich panels instead of plain log lines',
  )
  run_parser.set_handler(_command_run)

  subparser.add_parser('list', help='list registered bros').set_handler(_command_list)

  show_parser = subparser.add_parser('show', help='print an info card for a bro')
  show_parser.add_argument('name', help='bro name')
  show_parser.add_argument(
    '--system-prompt',
    action='store_true',
    help='also include the full assembled system prompt',
  )
  show_parser.set_handler(_command_show)

  return parser.dispatch(argv)
