from bro.base.args import Parser

DEFAULT_HOLD = 'guided'


def add_scope_flags(parser: Parser) -> None:
  """register the launch-scope overrides: the credential and summon-target
  adjustments layered onto a session's computed scope."""
  parser.add_argument(
    '--grant',
    action='append',
    default=None,
    metavar='NAME',
    help='add a credential (NAME) or a summonable bro (@BRO) to the session scope; a credential grant replaces the selected same-kind name (repeatable); errors on an exact duplicate or unknown name',
  )
  parser.add_argument(
    '--revoke',
    action='append',
    default=None,
    metavar='NAME',
    help='remove a credential (NAME) or a summonable bro (@BRO) from the session scope (repeatable); errors if not in the scope',
  )


def add_session_flags(parser: Parser, *, include_bro: bool = True) -> None:
  """register the session flags shared by `ride along` and the mode aliases and dive-in."""
  parser.add_argument(
    '--host',
    action='store_true',
    help='run on the host in a same-machine git worktree instead of the default isolated docker container',
  )
  # imported here, not at module level: llm pulls asyncio (~150ms) and this
  # module sits on every runtime CLI import
  from bro.launch.llm_flags import add_llm_flags
  from bro.llm.mcp import HOLDS

  # default None, not DEFAULT_HOLD: each wrapper resolves an omitted flag to
  # its own default, and reconstruction then always carries the resolved value
  parser.add_argument(
    '--hold',
    default=None,
    choices=HOLDS,
    help='how firmly the human holds the session: unattended = no human channel, detached = launched and left, '
    'attended = human watching while the work runs autonomously, guided = human drives each step. '
    'every level but guided skips permission prompts (unsandboxed when combined with --host). '
    'defaults: unattended for ride solo; attended for ride along and dive-in, guided with --host',
  )
  add_llm_flags(
    parser,
    effort_help='thinking effort level (forwarded to claude --effort)',
    fast_help='enable fast mode for the session (disabled by default regardless of host settings)',
  )
  add_scope_flags(parser)
  parser.add_argument(
    '--into',
    default=None,
    metavar='REF',
    help='base a new session on git REF (branch/tag/sha). a REF that only exists on origin is '
    'fetched automatically; ignored once the workspace exists. '
    "default: the host checkout's current HEAD for ride along; origin's freshly "
    'fetched HEAD for dive-in, with the host HEAD as the unreachable-origin fallback',
  )
  if include_bro:
    parser.add_argument(
      '--bro',
      default=None,
      help='the bro the session runs as (default: the project default bro)',
    )


def add_forwarded_flags(parser: Parser) -> None:
  add_session_flags(parser)
  from ride.claude.harness import add_flags

  add_flags(parser)


def extract_forwarded_argv(args: dict) -> list[str]:
  """pop forwarded-flag values from `args` and return them as canonical argv tokens.

  mutates `args`: removes every key registered by `add_forwarded_flags`. The returned
  list is suitable to splice directly into a `ride solo|along` invocation.
  """
  parser = Parser(add_help=False)
  add_forwarded_flags(parser)
  forwarded = {
    action.dest: args.pop(action.dest)
    for action in parser._actions
    if len(action.option_strings) > 0 and action.dest in args
  }
  return parser.reconstruct(forwarded, prog=[])
