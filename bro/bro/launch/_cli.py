"""shared CLI plumbing for `ask` / `do-task` / `call`: the scoped-container hop,
fast-mode bro construction, and the `ask`/`do-task` main()."""

import asyncio
import logging
import os
import secrets
import sys
from collections.abc import Callable, Coroutine
from typing import Optional

import base.args
from base import credentials
from bro.bro import BroRaised
from bro.bros.bro import Bro
from llm.observer import Observer

# shared flag help so all three CLIs describe `--slow` / `--no-container` identically.
# fast mode is the default for these CLIs; --slow opts back out to the plain spec.
SLOW_HELP = (
  "disable the bro's fast-mode LLM knob, which is on by default here "
  "(provider-specific; for ChatGPT fast mode is OpenAI's 'priority' service tier — "
  'same model and quality, faster and more consistent generation at a higher '
  'per-token price)'
)
NO_CONTAINER_HELP = 'skip the auto-container hop and run in the calling process'
NO_TRAILS_HELP = (
  'disable trails recording: set TRAILS_DISABLED in the container and drop the '
  'trails secret from the scoped set'
)
GRANT_HELP = (
  "grant a secret to the container's scoped set on top of the bro's manifest "
  '(repeatable); errors if it is already in the set or unknown to the registry'
)
REVOKE_HELP = (
  "revoke a secret from the container's scoped set (repeatable); errors if it is not in the set"
)


def create_bro_for_run(bro_name: str, *, fast: bool) -> Bro:
  """instantiate the bro for an in-process run. fast mode (the provider's fast knob)
  is the default for these CLIs; pass fast=False (`--slow`) for the plain spec.
  fast being implicit, a provider with no fast mode (e.g. echo) falls back to the
  normal spec rather than raising — the user never explicitly asked for fast."""
  from bro.registry import create_bro, get_class

  if not fast:
    return create_bro(bro_name)
  cls = get_class(bro_name)
  try:
    spec = cls.llm_spec.fast()
  except NotImplementedError:
    logging.getLogger(__name__).debug('%s has no fast mode; running with the normal spec', bro_name)
    return create_bro(bro_name)
  return cls.create(spec)


def maybe_containerize(
  *,
  cli_name: str,
  bro_name: str,
  inner_args: list[str],
  no_container: bool,
  no_trails: bool = False,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
) -> Optional[int]:
  """re-exec `<cli_name> <bro_name> <inner_args...>` inside a scoped throwaway
  container and return its exit code, or return None so the caller runs in the
  calling process.

  the hop is skipped when already inside a container (`CW_IN_CONTAINER`, set by the
  container) or when `--no-container` was passed — that is how the inner process
  avoids re-hopping and runs the bro in-process. otherwise the container is scoped
  to the bro's manifest: the bro runs as an LLM process here (not claude code), so
  add its llm key (`needed_secrets()` omits it) and `trails` (recording is mandatory
  for bro runs); the docker socket is granted only when the bro does docker work.
  an interactive surface (`call`) renders inside it just as claude code does.

  `no_trails` drops `trails` from the scoped set and sets `TRAILS_DISABLED` in the
  container (the in-container tracker factory then returns `NullTracker`).

  `grant`/`revoke` adjust that scoped set per `credentials.apply_grant_revoke`
  (strict: see its rules). they are host-side only — not threaded into the inner
  command — so passing them when the hop is skipped (`--no-container` / already
  in-container) is a no-op the caller didn't get, hence an error: host mode is
  unscoped and a revoke there would not actually restrict anything. returns 1
  (printing to stderr) on any grant/revoke misuse so the caller exits non-zero."""
  grant = grant if grant is not None else []
  revoke = revoke if revoke is not None else []
  if no_container or os.environ.get('CW_IN_CONTAINER') is not None:
    if len(grant) > 0 or len(revoke) > 0:
      print(
        '--grant/--revoke require containerization (not valid with --no-container)',
        file=sys.stderr,
      )
      return 1
    return None
  from bro.registry import create_bro
  from cw import run_in_container

  bro = create_bro(bro_name)
  needed = set(bro.needed_secrets()) | set(bro.llm_spec.needed_secrets())
  # the bro's best-effort tier (e.g. a data source's query-focused fetch summary).
  # a no-op for a bro whose optional secret is already its required LLM key, but
  # correct in general — a component that degrades without a secret still gets it
  # when the host can resolve it.
  optional = set(bro.optional_secrets())
  extra_env: dict[str, str] = {}
  if no_trails:
    extra_env['TRAILS_DISABLED'] = '1'
  else:
    needed |= {'trails'}
  try:
    needed = credentials.apply_grant_revoke(needed, grant=grant, revoke=revoke)
  except ValueError as e:
    print(str(e), file=sys.stderr)
    return 1
  workspace = f'{cli_name}-{bro_name}-{secrets.token_hex(4)}'
  command = [cli_name, bro_name, *inner_args]
  return run_in_container(
    workspace,
    command,
    drop=True,
    secrets=needed,
    optional_secrets=optional,
    docker_sock=bro.needs_docker,
    extra_env=extra_env,
    # this container runs its own named bro, so the calling session's ambient
    # CW_BRO must not leak in and mis-theme it.
    forward_bro=False,
  )


def run(
  *,
  cli_name: str,
  parser_desc: str,
  arg_name: str,
  arg_help: str,
  run_fn: Callable[[Bro, str, Optional[Observer]], Coroutine[None, None, str]],
  argv: list[str],
) -> Optional[int]:
  parser = base.args.Parser(description=parser_desc)
  parser.add_argument('bro', help='bro name')
  parser.add_argument(arg_name, help=arg_help)
  parser.add_argument(
    '--rich',
    action='store_true',
    help='render the trace as colored rich panels instead of plain log lines',
  )
  parser.add_argument('--slow', action='store_true', help=SLOW_HELP)
  parser.add_argument(
    '--no-container', dest='no_container', action='store_true', help=NO_CONTAINER_HELP
  )
  parser.add_argument('--no-trails', dest='no_trails', action='store_true', help=NO_TRAILS_HELP)
  # --no-trails acts only on the container hop; --no-container has no hop to act on.
  parser.add_exclusive_groups(['no_container'], ['no_trails'])
  parser.add_argument('--grant', action='append', default=None, metavar='SECRET', help=GRANT_HELP)
  parser.add_argument('--revoke', action='append', default=None, metavar='SECRET', help=REVOKE_HELP)
  args = parser.parse(argv)

  inner_args = [args[arg_name]]
  if args['rich']:
    inner_args.append('--rich')
  if args['slow']:
    inner_args.append('--slow')
  hopped = maybe_containerize(
    cli_name=cli_name,
    bro_name=args['bro'],
    inner_args=inner_args,
    no_container=args['no_container'],
    no_trails=args['no_trails'],
    grant=args['grant'],
    revoke=args['revoke'],
  )
  if hopped is not None:
    return hopped

  bro = create_bro_for_run(args['bro'], fast=not args['slow'])
  observer: Optional[Observer] = None
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
