#!/usr/bin/env python
import sys
from typing import Optional

from bro.base.args import REMAINDER, SUPPRESS, Parser
from bro.workspace.banner import banner
from bro.workspace.containers import exec_in_workspace
from bro.workspace.model import Workspace
from bro.workspace.paths import project_root
from ride.claude.harness import CLAUDE, ClaudeOptions
from ride.claude.runner import run_in_place
from ride.clean import clean_workspaces
from ride.flags import DEFAULT_HOLD, add_forwarded_flags, add_scope_flags
from ride.listing import list_workspaces
from ride.session import SessionSpec, resume_session, start_session

__cli_name__ = 'cw'


def build_parser() -> Parser:
  parser = Parser(description='launch claude in a managed workspace')
  subparsers = parser.add_subparsers(dest='cmd', required=True)

  ss = subparsers.add_parser('ss', help='start a claude session in a workspace')
  ss.add_argument(
    '--drop',
    action='store_true',
    help='remove the workspace after a clean exit (a failed session keeps it for inspection)',
  )
  # internal seam, not a user surface: the outer `cw ss` spawns `cw ss --in-place`
  # in a prepared workspace (the workspace's own cw), which runs the session from
  # its cwd — see ride/claude/runner.py
  ss.add_argument('--in-place', action='store_true', env=False, help=SUPPRESS)
  add_forwarded_flags(ss)
  # internal seams, not user surfaces: the ride outer serializes its mode into
  # the workspace checkout's compatibility runner — see ride/session.py
  ss.add_argument('--solo', action='store_true', env=False, help=SUPPRESS)
  ss.add_argument('--resume', action='store_true', env=False, help=SUPPRESS)
  ss.add_argument(
    '-p', '--prompt', default=None, help='initial prompt (prepended with base prompt)'
  )
  ss.add_argument('name', help='workspace name')
  ss.add_argument('claude_args', nargs=REMAINDER, help='args forwarded to claude')

  resume = subparsers.add_parser(
    'resume',
    help='resume the last claude session in a workspace, under the flags it ran with '
    '(--grant/--revoke adjust its scope)',
  )
  add_scope_flags(resume)
  resume.add_argument('name', help='workspace to resume, as `cw list` shows it')

  subparsers.add_parser('list', help='list workspaces ([.]=worktree, [o]=container, [x]=abandoned)')

  clean = subparsers.add_parser(
    'clean', help='remove stale workspaces that have no uncommitted or unpushed changes'
  )
  clean.add_argument(
    '--force',
    action='store_true',
    help='remove workspaces even if they have uncommitted or unpushed changes',
  )
  clean.add_argument(
    '--dry-run',
    action='store_true',
    help='show what would be removed without actually removing',
  )
  clean.add_argument('names', nargs='*', help='workspaces to clean (default: all)')

  check_clean = subparsers.add_parser(
    'check-clean',
    help='check if a workspace is safe to remove (exit 0=yes, 1=no); reasons printed to stderr',
  )
  check_clean.add_argument('name', help='workspace to check')

  exec_command = subparsers.add_parser(
    'exec',
    help='exec a command in the running container for a workspace (default: interactive bash with .venv activated)',
  )
  exec_command.add_argument('name', help='container workspace name')
  exec_command.add_argument(
    'command', nargs=REMAINDER, help='command + args to exec (default: bash)'
  )

  scope = subparsers.add_parser(
    'scope',
    help="print the credential scope a session launched from this project would hydrate, with the instance each kind reads (~/.bro.json's project selection)",
  )
  scope.add_argument(
    '--bro', default=None, help='the bro to scope for (default: the project default bro)'
  )
  scope.add_argument(
    '--raw', action='store_true', help='scope the --raw session flavor instead of a cw-session'
  )

  banner_parser = subparsers.add_parser(
    'banner',
    help='print the banner; auto-run by the container .bashrc on `cw exec` shells',
  )
  banner_parser.add_argument(
    '--llm',
    action='store_true',
    help='emit plain key:value lines for LLM Bash-tool consumption (no ANSI, no logo)',
  )

  return parser


def main(argv: list[str]) -> Optional[int]:
  parser = build_parser()
  args = parser.parse(argv)
  command = args.pop('cmd')

  if command == 'list':
    return list_workspaces()
  if command == 'resume':
    return resume_session(
      args['name'], interface='cw', grant=args['grant'] or [], revoke=args['revoke'] or []
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
    # imported here, not at module level: the launch-scope stack pulls the bro
    # registry, and every other subcommand runs without it
    from ride.scope_report import report_scope

    return report_scope(
      bro=args['bro'],
      raw=args['raw'],
      harness='claude',
      label='raw-session' if args['raw'] else 'cw-session',
    )
  if command == 'banner':
    return banner(llm=args['llm'])
  assert command == 'ss'
  in_place = args.pop('in_place')
  if in_place:
    # the inner-argv contract: the outer consumed the machinery flags, so any of
    # them here means a broken outer serialization, not a user mistake
    machinery = {
      '--host': args['host'],
      '--drop': args['drop'],
      '--grant': args['grant'] is not None,
      '--revoke': args['revoke'] is not None,
      '--into': args['into'] is not None,
    }
    offending = [flag for flag, present in machinery.items() if present]
    if len(offending) > 0:
      parser.error(f'--in-place cannot be combined with {", ".join(offending)}')
  if args['solo'] and not in_place:
    parser.error('--solo is an inner-argv token')
  if args['resume'] and not in_place:
    parser.error('resuming is `cw resume <workspace>`; --resume is an inner-argv token')
  # the outer-only --raw × --host gate is skipped under --in-place: the outer
  # validated it once, and the inner argv never carries --host
  if args['raw'] and args['host'] and not in_place:
    parser.error('--raw cannot be combined with --host (the raw flavor is fenced to the container)')
  if args['hold'] is None:
    args['hold'] = DEFAULT_HOLD
  # imported here, not at module level: the flag layer pulls the llm package,
  # and every other subcommand runs without it
  from bro.launch.llm_flags import canonicalize, drop_piece_flags, selection_from_args
  from bro.llm.providers import LLMSelectionError

  try:
    canonicalize(args, selection_from_args(args))
    drop_piece_flags(args)
    bro_argument = args.pop('bro')
    raw = args.pop('raw')
    claude_arguments = args.pop('claude_args')
    args['grant'] = args['grant'] or []
    args['revoke'] = args['revoke'] or []
    from bro.workspace.project import project_config

    bro_name = bro_argument if bro_argument is not None else project_config().default_bro
    resolved_llm = CLAUDE.resolve_llm(args['llm'], bro_name)
    spec = SessionSpec(
      interface='cw',
      harness='claude',
      workspace_pinned=True,
      bro=bro_name,
      bro_argument=bro_argument,
      resolved_llm=resolved_llm.dump(),
      harness_options=ClaudeOptions(raw=raw, arguments=claude_arguments).dump(),
      **args,
    )
  except LLMSelectionError as error:
    parser.error(str(error))
  if in_place:
    return run_in_place(spec)
  return start_session(spec)
