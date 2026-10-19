"""Promise the session's watch store another beat from a producer's shell loop."""

import sys

import bro.base.args as base_args
from bro import watches

__cli_name__ = 'watch-beat'


def main(argv: list[str]) -> int:
  parser = base_args.Parser(
    description='promise this watch another beat within the given seconds; '
    'does nothing outside a watch producer'
  )
  parser.add_argument('within_seconds', type=float, help='seconds within which the next beat comes')
  arguments = parser.parse(argv)
  try:
    watches.beat(arguments['within_seconds'])
  except ValueError as error:
    parser.error(str(error))
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
