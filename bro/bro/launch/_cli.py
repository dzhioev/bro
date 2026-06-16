"""shared main() for `ask` and `do-task`: parse, re-exec into a container, or run the bro."""

import asyncio
import os
import secrets
import sys
from typing import Callable, Coroutine

import base.args
from bro.bro import BroRaised
from bro.bros.bro import Bro
from llm.observer import Observer


def run(
  *,
  cli_name: str,
  parser_desc: str,
  arg_name: str,
  arg_help: str,
  run_fn: Callable[[Bro, str, Observer | None], Coroutine[None, None, str]],
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
    from bro.registry import create_bro
    from cw import run_in_container

    # scope the throwaway container to this bro's manifest. the bro runs as an LLM
    # process here (not claude code), so add its llm key (`needed_secrets()` omits
    # it) and `trails` (recording is mandatory for bro runs). `aws` in the set is
    # delivered like any secret; the docker socket only when the bro does docker work.
    bro = create_bro(args['bro'])
    needed = set(bro.needed_secrets()) | set(bro.llm_spec.needed_secrets()) | {'trails'}
    workspace = f'{cli_name}-{args["bro"]}-{secrets.token_hex(4)}'
    inner = [cli_name, args['bro'], args[arg_name]]
    if args['rich']:
      inner.append('--rich')
    return run_in_container(
      workspace, inner, drop=True, secrets=needed, docker_sock=bro.needs_docker
    )

  from bro.registry import create_bro

  bro = create_bro(args['bro'])
  observer: Observer | None = None
  if args['rich']:
    from llm.observer import RichConsoleRenderer

    observer = RichConsoleRenderer(prefix=bro.name)
  try:
    result = asyncio.run(run_fn(bro, args[arg_name], observer))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  except KeyError as e:
    # raised by bro.get_skill_body when the `/<name>` prefix in input names
    # a skill the bro does not expose; the message includes the available list.
    print(str(e.args[0]) if len(e.args) > 0 else str(e), file=sys.stderr)
    return 1
  print(result)
