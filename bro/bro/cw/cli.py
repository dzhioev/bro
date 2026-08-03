#!/usr/bin/env python
import sys
from typing import Optional

from bro.base.args import REMAINDER, SUPPRESS, Parser
from bro.cw.claude_auth import _load_anthropic_key
from bro.cw.clean import clean_workspaces
from bro.cw.flags import DEFAULT_HOLD, add_forwarded_flags
from bro.cw.listing import list_workspaces
from bro.cw.runner import run_in_place
from bro.cw.session import SessionSpec, start_session
from bro.workspace.banner import banner
from bro.workspace.containers import exec_in_workspace
from bro.workspace.model import Workspace
from bro.workspace.paths import project_root

__cli_name__ = 'cw'


def build_parser() -> Parser:
  parser = Parser(description='launch claude with worktree management')
  subparsers = parser.add_subparsers(dest='cmd', required=True)

  ss = subparsers.add_parser('ss', help='start a claude session in a worktree')
  ss.add_argument(
    '--drop',
    action='store_true',
    help='remove the workspace after a clean exit (a failed session keeps it for inspection)',
  )
  # internal seam, not a user surface: the outer `cw ss` spawns `cw ss --in-place`
  # in a prepared workspace (the workspace's own cw), which runs the session from
  # its cwd — see cw/runner.py
  ss.add_argument('--in-place', action='store_true', env=False, help=SUPPRESS)
  add_forwarded_flags(ss)
  ss.add_argument(
    '--resume',
    action='store_true',
    help='resume the latest claude session in the named workspace; skips the initial prompt',
  )
  ss.add_argument(
    '-p', '--prompt', default=None, help='initial prompt (prepended with base prompt)'
  )
  ss.add_argument('name', help='worktree name')
  ss.add_argument('claude_args', nargs=REMAINDER, help='args forwarded to claude')

  subparsers.add_parser('list', help='list workspaces ([.]=local, [o]=container, [x]=abandoned)')

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
  clean.add_argument(
    'refs',
    nargs='*',
    help='workspaces to clean (default: all); use c:<name> for container workspaces',
  )

  check_clean = subparsers.add_parser(
    'check-clean',
    help='check if a workspace is safe to remove (exit 0=yes, 1=no); reasons printed to stderr',
  )
  check_clean.add_argument(
    'ref',
    help='workspace to check; use c:<name> for container workspaces',
  )

  exec_command = subparsers.add_parser(
    'exec',
    help='exec a command in the running container for a workspace (default: interactive bash with .venv activated)',
  )
  exec_command.add_argument(
    'name', help="container workspace name (the 'c:' prefix is accepted but optional)"
  )
  exec_command.add_argument(
    'command', nargs=REMAINDER, help='command + args to exec (default: bash)'
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
  if command == 'clean':
    return clean_workspaces(force=args['force'], dry_run=args['dry_run'], refs=args['refs'])
  if command == 'check-clean':
    try:
      workspace = Workspace.from_ref(args['ref'], project_root())
    except ValueError as e:
      print(str(e), file=sys.stderr)
      return 1
    clean_, reasons = workspace.is_clean()
    for r in reasons:
      print(r, file=sys.stderr)
    return 0 if clean_ else 1
  if command == 'exec':
    return exec_in_workspace(name=args['name'], command=args['command'])
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
  # the outer-only policy gates (--raw × --host, the anthropic-key probe) are
  # skipped under --in-place: the outer validated them once, and the inner argv
  # never carries --host
  if args['into'] is not None and args['resume']:
    parser.error(
      '--into cannot be combined with --resume (it only applies when creating a workspace)'
    )
  if args['raw'] and not in_place:
    if args['host']:
      parser.error(
        '--raw cannot be combined with --host (the raw flavor is fenced to the container)'
      )
    if _load_anthropic_key() is None:
      parser.error(
        '--raw requires the `anthropic` secret to provide an api_key '
        '({"api_key": "..."}); claude --bare does not use OAuth or keychain'
      )
  if args['resume']:
    if args['drop']:
      parser.error('--resume cannot be combined with --drop')
    if args['prompt'] is not None:
      parser.error(
        '--resume cannot be combined with -p/--prompt (the initial prompt is ignored on resume)'
      )
  if args['hold'] is None:
    args['hold'] = DEFAULT_HOLD
  spec = SessionSpec(**args)
  if in_place:
    return run_in_place(spec)
  return start_session(spec)
