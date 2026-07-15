import asyncio
from typing import Optional

import base.args

__cli_name__ = 'bro'


def _command_list() -> None:
  from bro.registry import list_classes

  for bro_class in list_classes():
    print(f'{bro_class.name}: {bro_class.description}')


def _command_show(name: str, system_prompt: bool) -> None:
  from bro.registry import create_bro
  from bro.show import format_card

  bro = create_bro(name)
  card = asyncio.run(format_card(bro, include_system_prompt=system_prompt))
  print(card, end='')


def _launcher_invocation(argv: list[str]) -> Optional[tuple[str, list[str]]]:
  command_index = 1
  while command_index < len(argv) and argv[command_index].startswith('-'):
    command_index += 1
  if command_index >= len(argv) or argv[command_index] not in ('run', 'chat'):
    return None
  return argv[command_index], [argv[0], *argv[1:command_index], *argv[command_index + 1 :]]


def main(argv: list[str]) -> Optional[int]:
  launcher = _launcher_invocation(argv)
  if launcher is not None and launcher[0] == 'run':
    from bro.launch._cli import run_main

    return run_main(launcher[1], program=['bro', 'run'])
  if launcher is not None and launcher[0] == 'chat':
    from bro.launch.call import chat_main

    return chat_main(launcher[1], program=['bro', 'chat'])

  parser = base.args.Parser(description='inspect and launch bro agents')
  subparser = parser.add_subparsers(dest='command')
  subparser.add_parser('run', help='run a bro on a single input')
  subparser.add_parser('chat', help='open an interactive session with a bro')
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
