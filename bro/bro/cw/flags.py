from base.args import Parser

EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')


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
  parser.add_argument(
    '--auto',
    action='store_true',
    help='let claude run autonomously, skipping most permissions (unsandboxed when combined with --host)',
  )
  parser.add_argument(
    '--fast',
    action='store_true',
    help='enable fast mode for the session (disabled by default regardless of host settings)',
  )
  parser.add_argument(
    '--grant-cred',
    action='append',
    default=None,
    metavar='SECRET',
    help='grant a secret to the session scoped set on top of the computed set (repeatable); errors if already in the set or unknown to the registry',
  )
  parser.add_argument(
    '--revoke-cred',
    action='append',
    default=None,
    metavar='SECRET',
    help='revoke a secret from the session scoped set (repeatable); errors if not in the set',
  )
  parser.add_argument(
    '--grant-summon',
    action='append',
    default=None,
    metavar='BRO',
    help="allow the session to summon the named bro, on top of its bro's may_summon defaults (repeatable); errors if already allowed or not a registered bro",
  )
  parser.add_argument(
    '--revoke-summon',
    action='append',
    default=None,
    metavar='BRO',
    help='disallow summoning the named bro for this session (repeatable); errors if not in the allow-list',
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
