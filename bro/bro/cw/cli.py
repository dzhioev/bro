#!/usr/bin/env python
import sys
from pathlib import Path
from typing import Optional

from base.args import REMAINDER, Parser
from cw.banner import banner
from cw.bro import _populate_bro_skills
from cw.clean import clean_workspaces
from cw.containers import exec_in_workspace
from cw.flags import add_forwarded_flags
from cw.listing import list_workspaces
from cw.paths import _project_root
from cw.secrets import _load_anthropic_key
from cw.session import SessionSpec, start_session
from cw.workspace import Workspace, _host_path_is_clean

__cli_name__ = 'cw'


def build_parser() -> Parser:
  parser = Parser(description='launch claude with worktree management')
  subparsers = parser.add_subparsers(dest='cmd', required=True)

  ss = subparsers.add_parser('ss', help='start a claude session in a worktree')
  ss.add_argument(
    '-c', '--container', action='store_true', help='run claude inside an isolated docker container'
  )
  ss.add_argument(
    '--drop', action='store_true', help='remove the workspace on exit without prompting'
  )
  add_forwarded_flags(ss)
  ss.add_argument(
    '--mcp',
    nargs='?',
    const='http',
    default=None,
    choices=['http', 'local'],
    help='connect flow MCP tools: http (default) uses the deployed server, local serves this checkout via a session-local HTTP server',
  )
  ss.add_argument(
    '--bro',
    default=None,
    help="start a clean claude session with the named bro's persona (system prompt, MCP servers, tools); requires -c and the `anthropic` secret; mutually exclusive with --mcp, --auto",
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
    help='check if a workspace is clean (exit 0=clean, 1=not); reasons printed to stderr',
  )
  check_clean.add_argument(
    'ref',
    nargs='?',
    help='workspace to check (default: cwd); use c:<name> for container workspaces',
  )

  exec_cmd = subparsers.add_parser(
    'exec',
    help='exec a command in the running container for a workspace (default: interactive bash with .venv activated)',
  )
  exec_cmd.add_argument(
    'name', help="container workspace name (the 'c:' prefix is accepted but optional)"
  )
  exec_cmd.add_argument('command', nargs=REMAINDER, help='command + args to exec (default: bash)')

  populate = subparsers.add_parser(
    'populate-bro-skills',
    help="symlink the named bro's skills into .claude/skills/ for Claude Code slash-command discovery (run from the --bro container entrypoint)",
  )
  populate.add_argument('bro_name', help='registered bro name (e.g. ppp-dev)')

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
  cmd = args.pop('cmd')

  if cmd == 'list':
    return list_workspaces()
  if cmd == 'clean':
    return clean_workspaces(force=args['force'], dry_run=args['dry_run'], refs=args['refs'])
  if cmd == 'check-clean':
    ref = args['ref']
    if ref is None:
      clean_, reasons = _host_path_is_clean(Path.cwd())
    else:
      try:
        ws = Workspace.from_ref(ref, _project_root())
      except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
      clean_, reasons = ws.is_clean()
    for r in reasons:
      print(r, file=sys.stderr)
    return 0 if clean_ else 1
  if cmd == 'exec':
    return exec_in_workspace(name=args['name'], cmd=args['command'])
  if cmd == 'populate-bro-skills':
    _populate_bro_skills(_project_root(), args['bro_name'])
    return 0
  if cmd == 'banner':
    return banner(llm=args['llm'])
  assert cmd == 'ss'
  if args['auto'] and not args['container']:
    parser.error('--auto requires --container')
  if args['into'] is not None and args['resume']:
    parser.error(
      '--into cannot be combined with --resume (it only applies when creating a workspace)'
    )
  if args['bro'] is not None:
    if not args['container']:
      parser.error('--bro requires --container')
    if args['auto']:
      parser.error('--bro cannot be combined with --auto')
    if args['mcp'] is not None:
      parser.error('--bro cannot be combined with --mcp (the bro defines its own MCP servers)')
    if _load_anthropic_key() is None:
      parser.error(
        '--bro requires the `anthropic` secret to provide an api_key '
        '({"api_key": "..."}); claude --bare does not use OAuth or keychain'
      )
  if args['resume']:
    if args['drop']:
      parser.error('--resume cannot be combined with --drop')
    if args['prompt'] is not None:
      parser.error(
        '--resume cannot be combined with -p/--prompt (the initial prompt is ignored on resume)'
      )
  if (args['grant'] is not None or args['revoke'] is not None) and not args['container']:
    parser.error(
      '--grant/--revoke require -c/--container: host mode is unscoped, so a revoke '
      'could not actually restrict the session'
    )
  return start_session(SessionSpec(**args))
