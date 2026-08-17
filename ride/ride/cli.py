#!/usr/bin/env python
import sys
from typing import Optional

from bro.base.args import REMAINDER, Parser
from bro.launch.llm_flags import canonicalize, drop_piece_flags, selection_from_args
from bro.llm.providers import LLMSelectionError
from bro.workspace.banner import banner
from bro.workspace.containers import exec_in_workspace
from bro.workspace.model import Workspace
from bro.workspace.paths import fresh_workspace_name, project_root
from bro.workspace.project import project_config
from ride.claude.harness import ClaudeOptions, add_flags as add_claude_flags
from ride.clean import clean_workspaces
from ride.flags import add_scope_flags, add_session_flags
from ride.harness import get_harness
from ride.listing import list_workspaces
from ride.session import SessionSpec, resume_session, start_session

__cli_name__ = 'ride'


def build_parser() -> Parser:
  parser = Parser(description='run a harness infused with a bro in a managed workspace')
  subparsers = parser.add_subparsers(dest='cmd', required=True)

  along = subparsers.add_parser('along', help='start an interactive session')
  along.add_argument(
    '-w',
    '--workspace',
    default=None,
    metavar='NAME',
    help='pin or reuse NAME (pinned workspaces are always kept)',
  )
  along.add_argument(
    '--drop',
    action='store_true',
    help='remove an automatically named workspace after a clean exit',
  )
  along.add_argument(
    '--harness',
    choices=('claude', 'bro'),
    default=None,
    help='driving harness (default: project [tool.bro] harness, then claude; bro is reserved)',
  )
  add_session_flags(along, include_bro=False)
  add_claude_flags(along)
  along.add_argument('bro', help='bro personality to run the harness as')
  along.add_argument('prompt', nargs='?', default=None, help='initial prompt')

  resume = subparsers.add_parser(
    'resume', help='resume the last harness session in a workspace under its recorded recipe'
  )
  add_scope_flags(resume)
  resume.add_argument('name', help='workspace to resume, as `ride list` shows it')

  subparsers.add_parser('list', help='list workspaces ([.]=worktree, [o]=container, [x]=abandoned)')

  clean = subparsers.add_parser(
    'clean', help='remove stale workspaces that have no uncommitted or unpushed changes'
  )
  clean.add_argument('--force', action='store_true', help='remove workspaces even when dirty')
  clean.add_argument('--dry-run', action='store_true', help='show removals without applying them')
  clean.add_argument('names', nargs='*', help='workspaces to clean (default: all)')

  check_clean = subparsers.add_parser(
    'check-clean', help='check whether a workspace is safe to remove (exit 0=yes, 1=no)'
  )
  check_clean.add_argument('name', help='workspace to check')

  exec_command = subparsers.add_parser(
    'exec', help='exec a command in a running container workspace (default: interactive bash)'
  )
  exec_command.add_argument('name', help='container workspace name')
  exec_command.add_argument('command', nargs=REMAINDER, help='command and arguments')

  scope = subparsers.add_parser('scope', help='print a prospective session credential scope')
  scope.add_argument('--bro', default=None, help='bro to scope (default: project default)')
  scope.add_argument('--harness', choices=('claude', 'bro'), default=None)
  scope.add_argument('--raw', action='store_true', help='scope Claude raw mode')

  banner_parser = subparsers.add_parser('banner', help='print the session banner')
  banner_parser.add_argument('--llm', action='store_true', help='emit plain key:value lines')
  return parser


def _parse(parser: Parser, argv: list[str]) -> tuple[dict, list[str]]:
  if len(argv) < 2 or argv[1] != 'along':
    return parser.parse(argv), []
  try:
    separator = argv.index('--')
  except ValueError:
    return parser.parse(argv), []
  return parser.parse(argv[:separator]), argv[separator + 1 :]


def _along(parser: Parser, args: dict, harness_arguments: list[str]) -> int:
  if args['workspace'] is not None and args['drop']:
    parser.error('--drop cannot be combined with --workspace; pinned workspaces are always kept')
  if args['hold'] is None:
    args['hold'] = 'guided' if args['host'] else 'attended'
  harness_name = args.pop('harness') or project_config().harness
  if harness_name == 'bro':
    parser.error('the bro harness is not implemented yet; use --harness claude')
  try:
    get_harness(harness_name)
    canonicalize(args, selection_from_args(args))
    drop_piece_flags(args)
  except (LLMSelectionError, ValueError) as error:
    parser.error(str(error))
  raw = args.pop('raw')
  args['grant'] = args['grant'] or []
  args['revoke'] = args['revoke'] or []
  if raw and args['host']:
    parser.error('--raw cannot be combined with --host')
  workspace = args.pop('workspace')
  bro = args.pop('bro')
  prompt = args.pop('prompt')
  name = workspace if workspace is not None else fresh_workspace_name(f'ride-{bro}')
  spec = SessionSpec(
    name=name,
    interface='ride',
    harness=harness_name,
    workspace_pinned=workspace is not None,
    bro=bro,
    bro_argument=bro,
    prompt=prompt,
    resume=False,
    harness_options=ClaudeOptions(raw=raw, arguments=harness_arguments).dump(),
    **args,
  )
  try:
    _ = spec.llm_spec
  except LLMSelectionError as error:
    parser.error(str(error))
  return start_session(spec)


def main(argv: list[str]) -> Optional[int]:
  parser = build_parser()
  args, harness_arguments = _parse(parser, argv)
  command = args.pop('cmd')
  if command != 'along' and len(harness_arguments) > 0:
    parser.error('`--` harness arguments are accepted only by `ride along`')
  if command == 'along':
    return _along(parser, args, harness_arguments)
  if command == 'list':
    return list_workspaces()
  if command == 'resume':
    return resume_session(
      args['name'], interface='ride', grant=args['grant'] or [], revoke=args['revoke'] or []
    )
  if command == 'clean':
    return clean_workspaces(force=args['force'], dry_run=args['dry_run'], names=args['names'])
  if command == 'check-clean':
    try:
      workspace = Workspace.open(args['name'], project_root())
    except ValueError as error:
      print(str(error), file=sys.stderr)
      return 1
    clean, reasons = workspace.is_clean()
    for reason in reasons:
      print(reason, file=sys.stderr)
    return 0 if clean else 1
  if command == 'exec':
    return exec_in_workspace(name=args['name'], command=args['command'])
  if command == 'scope':
    from ride.scope_report import report_scope

    return report_scope(bro=args['bro'], harness=args['harness'], raw=args['raw'])
  assert command == 'banner'
  return banner(llm=args['llm'])
