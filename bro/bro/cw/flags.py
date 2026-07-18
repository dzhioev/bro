from base.args import Parser

DEFAULT_SESSION_MODE = 'attended'


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
  from llm.llm import EFFORT_LEVELS
  from llm.mcp import MODES

  parser.add_argument(
    '--mode',
    default=DEFAULT_SESSION_MODE,
    choices=MODES,
    help='user-involvement level: unattended = no human channel, detached = launched and left, '
    'attended = human watching while the work runs autonomously (default), guided = human drives '
    'each step. every level but guided skips permission prompts (unsandboxed when combined with '
    '--host)',
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
    help='add a credential (NAME) or a summonable bro (@BRO) to the session scope on top of the computed set (repeatable); errors if already in the scope or unknown',
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
    help="base a new session on git REF (branch/tag/sha) instead of the host checkout's current HEAD (the default in both container and host mode). a REF that only exists on origin is fetched automatically. ignored once the workspace exists",
  )
  parser.add_argument(
    '--persona',
    default=None,
    metavar='BRO',
    help='the bro a cw-session is themed as — persona prompt injection, skills as slash commands, and the session-local MCP namespaces (default: the project default bro); mutually exclusive with --bro',
  )
  parser.add_argument(
    '--bro',
    default=None,
    help="run a clean claude session as the named bro's persona (system prompt, MCP servers, tools); container only (rejected with --host), requires the `anthropic` secret; mutually exclusive with --persona",
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
