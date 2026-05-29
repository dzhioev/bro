"""shared main() for `ask` and `do-task`: parse, re-exec into a container, or run the bro."""

import asyncio
import os
import secrets
import sys
from typing import Callable, Coroutine

import base.args
from bro.bro import BaseBro, BroRaised
from llm.tracer import Tracer


def run(
  *,
  cli_name: str,
  parser_desc: str,
  arg_name: str,
  arg_help: str,
  run_fn: Callable[[BaseBro, str, Tracer | None], Coroutine[None, None, str]],
  argv: list[str] | None,
) -> int | None:
  parser = base.args.Parser(description=parser_desc)
  parser.add_argument('bro', help='bro name')
  parser.add_argument(arg_name, help=arg_help)
  parser.add_argument(
    '--rich',
    action='store_true',
    help='render the trace as colored rich panels instead of plain log lines',
  )
  parser.add_argument(
    '--no-container',
    dest='no_container',
    action='store_true',
    help='skip the auto-container hop and run the bro in the calling process',
  )
  args = parser.parse(argv)

  if os.environ.get('CW_IN_CONTAINER') is None and not args['no_container']:
    from cw import run_in_container

    workspace = f'{cli_name}-{args["bro"]}-{secrets.token_hex(4)}'
    inner = [cli_name, args['bro'], args[arg_name]]
    if args['rich']:
      inner.append('--rich')
    return run_in_container(workspace, inner, drop=True)

  from bro.registry import get_bro

  bro = get_bro(args['bro'])
  tracer: Tracer | None = None
  if args['rich']:
    from llm.tracer import RichConsoleTracer

    tracer = RichConsoleTracer(prefix=bro.name)
  try:
    result = asyncio.run(run_fn(bro, args[arg_name], tracer))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  except KeyError as e:
    # raised by bro.get_skill_body when the `/<name>` prefix in input names
    # a skill the bro does not expose; the message includes the available list.
    print(str(e.args[0]) if len(e.args) > 0 else str(e), file=sys.stderr)
    return 1
  print(result)
