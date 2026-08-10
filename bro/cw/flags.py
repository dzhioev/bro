from bro.base.args import Parser

DEFAULT_HOLD = 'guided'


def add_forwarded_flags(parser: Parser) -> None:
  """register the flags that the dive-in wrapper forwards to `cw ss`.

  Adding a new pass-through flag here makes it available in every wrapper that
  calls this helper — no per-flag plumbing in each wrapper.
  """
  parser.add_argument(
    '--host',
    action='store_true',
    help='run on the host in a same-machine git worktree instead of the default isolated docker container',
  )
  # imported here, not at module level: llm pulls asyncio (~150ms) and this
  # module sits on every `import cw`
  from bro.llm.llm import EFFORT_LEVELS
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
    'default: guided for cw ss; attended for dive-in, guided for dive-in --host',
  )
  parser.add_argument(
    '--fast',
    action='store_true',
    help='enable fast mode for the session (disabled by default regardless of host settings)',
  )
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
  parser.add_argument(
    '--effort',
    default='xhigh',
    choices=EFFORT_LEVELS,
    help='thinking effort level (forwarded to claude --effort); defaults to xhigh',
  )
  parser.add_argument(
    '--into',
    default=None,
    metavar='REF',
    help='base a new session on git REF (branch/tag/sha). a REF that only exists on origin is '
    'fetched automatically; ignored once the workspace exists. '
    "default: the host checkout's current HEAD for cw ss; origin's freshly fetched HEAD for "
    'dive-in, with the host HEAD as the unreachable-origin fallback',
  )
  parser.add_argument(
    '--bro',
    default=None,
    help='the bro the session runs as — persona and script prompt injection, canonical script tools, and the session-local MCP namespaces (default: the project default bro)',
  )
  parser.add_argument(
    '--raw',
    action='store_true',
    help="run the session as bare claude under its bro's own system prompt and MCP toolset instead of the Claude Code harness; container only (rejected with --host), requires the `anthropic` secret",
  )


def extract_forwarded_argv(args: dict) -> list[str]:
  """pop forwarded-flag values from `args` and return them as canonical argv tokens.

  mutates `args`: removes every key registered by `add_forwarded_flags`. The returned
  list is suitable to splice directly into a `cw ss` invocation.
  """
  parser = Parser(add_help=False)
  add_forwarded_flags(parser)
  forwarded = {
    a.dest: args.pop(a.dest)
    for a in parser._actions
    if len(a.option_strings) > 0 and a.dest in args
  }
  return parser.reconstruct(forwarded, prog=[])
