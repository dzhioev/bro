from bro.base.args import Parser
from ride.harness import HARNESS_NAMES, get_harness
from ride.workspace.metadata import Isolation


def default_hold(*, solo: bool, isolation: Isolation) -> str:
  """The hold an omitted --hold resolves to."""
  return 'unattended' if solo else 'guided' if isolation is Isolation.UNBOXED else 'attended'


def isolation_from_args(args: dict) -> Isolation:
  boxed = args.pop('boxed')
  unboxed = args.pop('unboxed')
  if boxed and unboxed:
    raise ValueError('--boxed and --unboxed are mutually exclusive')
  return Isolation.UNBOXED if unboxed else Isolation.BOXED


def add_harness_flags(parser: Parser) -> None:
  """Register `--harness` and every harness's own flags."""
  parser.add_argument(
    '--harness',
    choices=HARNESS_NAMES,
    default=None,
    help='driving harness (default: project [tool.bro] harness, then claude)',
  )
  for name in HARNESS_NAMES:
    get_harness(name).add_flags(parser)


def _harness_flag_defaults() -> dict[str, dict]:
  """Per harness, the flag dests it registers with their parser defaults."""
  defaults: dict[str, dict] = {}
  for name in HARNESS_NAMES:
    scratch = Parser(add_help=False)
    dests = get_harness(name).add_flags(scratch)
    by_dest = {action.dest: action.default for action in scratch._actions}
    defaults[name] = {dest: by_dest[dest] for dest in dests}
  return defaults


def pop_harness_options(
  parser: Parser, args: dict, harness_name: str, *, solo: bool, isolation: Isolation
) -> dict:
  """Pop every harness's flag values out of `args` and pack the selected one."""
  if harness_name not in HARNESS_NAMES:
    parser.error(f'unknown harness: {harness_name}')
  packed: dict = {}
  for name, flag_defaults in _harness_flag_defaults().items():
    values = {dest: args.pop(dest) for dest in flag_defaults}
    if name == harness_name:
      try:
        packed = get_harness(name).parse_options(values, solo=solo, isolation=isolation)
      except ValueError as error:
        parser.error(str(error))
      continue
    for dest, default in flag_defaults.items():
      if values[dest] != default:
        parser.error(f'--{dest.replace("_", "-")} requires --harness {name}')
  return packed


def add_scope_flags(parser: Parser) -> None:
  """Register launch-scope credential and summon-target adjustments."""
  parser.add_argument(
    '--grant',
    action='append',
    default=None,
    metavar='NAME',
    help='add a credential (KIND or KIND+INSTANCE) or a summonable bro (@BRO) to the session scope; an instance grant replaces the kind selection (repeatable)',
  )
  parser.add_argument(
    '--revoke',
    action='append',
    default=None,
    metavar='NAME',
    help='remove a credential kind (KIND) or a summonable bro (@BRO) from the session scope (repeatable); credential instances cannot be revoked directly',
  )


def add_session_flags(parser: Parser, *, include_bro: bool = True) -> None:
  """Register the session flags shared by mode aliases and dive-in."""
  isolation = parser.add_mutually_exclusive_group()
  isolation.add_argument(
    '--boxed',
    action='store_true',
    help='run in a dedicated container (the default)',
  )
  isolation.add_argument(
    '--unboxed',
    action='store_true',
    help="run the workspace tree directly on the launcher's filesystem",
  )
  from bro.launch.llm_flags import add_llm_flags
  from bro.mcp import HOLDS

  parser.add_argument(
    '--hold',
    default=None,
    choices=HOLDS,
    help='how firmly the human holds the session: unattended = no human channel, detached = launched and left, '
    'attended = human watching while the work runs autonomously, guided = human drives each step. '
    'every level but guided skips permission prompts (unsandboxed when combined with --unboxed). '
    'defaults: unattended for ride solo; attended for boxed ride along and dive-in, guided when unboxed',
  )
  add_llm_flags(
    parser,
    effort_help='thinking effort level (forwarded to claude --effort)',
    fast_help='enable fast mode for the session (disabled by default regardless of host settings)',
  )
  add_scope_flags(parser)
  parser.add_argument(
    '--no-trails',
    dest='no_trails',
    action='store_true',
    help='disable trail recording for the session',
  )
  parser.add_argument(
    '--into',
    default=None,
    metavar='REF',
    help='base a new attached session on git REF (branch/tag/sha). a REF that only exists on '
    'origin is fetched automatically; ignored once the workspace exists. default: the local '
    "checkout's current HEAD or a git URL's freshly fetched origin/HEAD",
  )
  if include_bro:
    parser.add_argument(
      '--bro',
      default=None,
      help='the bro the session runs as (default: the project default bro)',
    )


def add_forwarded_flags(parser: Parser) -> None:
  add_session_flags(parser)
  add_harness_flags(parser)


def extract_forwarded_argv(args: dict) -> list[str]:
  """Pop forwarded-flag values from `args` and return canonical argv tokens."""
  parser = Parser(add_help=False)
  add_forwarded_flags(parser)
  forwarded = {
    action.dest: args.pop(action.dest)
    for action in parser._actions
    if len(action.option_strings) > 0 and action.dest in args
  }
  return parser.reconstruct(forwarded, prog=[])
