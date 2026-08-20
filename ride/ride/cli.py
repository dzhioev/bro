#!/usr/bin/env python
import functools
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.base.args import REMAINDER, SUPPRESS, Parser
from bro.launch.llm_flags import canonicalize, drop_piece_flags, selection_from_args
from bro.llm.providers import LLMSelectionError
from bro.workspace.banner import banner
from bro.workspace.paths import RuntimeLocationError, fresh_workspace_name, project_root
from bro.workspace.project import project_config
from ride.clean import clean_workspaces
from ride.flags import (
  add_harness_flags,
  add_scope_flags,
  add_session_flags,
  default_hold,
  pop_harness_options,
)
from ride.harness import get_harness
from ride import pending_summon
from ride.listing import list_workspaces
from ride.repository import Repository, is_git_url, resolve_repository
from ride.session import SessionSpec, resume_session, start_session
from ride.workspace.containers import exec_in_workspace
from ride.workspace.model import Workspace

__cli_name__ = 'ride'

_Main = Callable[[list[str]], Optional[int]]


def reports_location_errors(main: _Main) -> _Main:
  """wrap a console-script main so an unusable runtime location fails as a CLI
  error rather than a traceback."""

  @functools.wraps(main)
  def wrapper(argv: list[str]) -> Optional[int]:
    try:
      return main(argv)
    except RuntimeLocationError as error:
      log.error('%s', error)
      return 1

  return wrapper


def _add_mode_flags(parser: Parser) -> None:
  parser.add_argument(
    '--repo',
    default=None,
    metavar='PATH|URL',
    help='attach an existing checkout path or git URL (default: detached)',
  )
  parser.add_argument(
    '-w',
    '--workspace',
    default=None,
    metavar='NAME',
    help='pin or reuse NAME (pinned workspaces are always kept)',
  )
  add_harness_flags(parser)
  add_session_flags(parser, include_bro=False)
  parser.add_argument('--in-place', action='store_true', env=False, help=SUPPRESS)
  parser.add_argument('--resume', action='store_true', env=False, help=SUPPRESS)


def _configure_mode_parser(parser: Parser, *, solo: bool) -> None:
  _add_mode_flags(parser)
  if solo:
    parser.add_argument(
      '--keep',
      action='store_true',
      help='keep an automatically named workspace after a clean exit',
    )
  else:
    parser.add_argument(
      '--drop',
      action='store_true',
      help='remove an automatically named workspace after a clean exit',
    )
    parser.add_argument(
      '--summoned',
      default=None,
      metavar='TOKEN',
      help='attach this session as the manual summon child TOKEN names: the pending '
      'record fixes the bro, the initial prompt, and the base, and the answer goes '
      'back to the waiting summoner',
    )
  parser.add_argument('bro', help='bro personality to run the harness as')
  if solo:
    parser.add_argument('prompt', help='prompt to answer')
  else:
    parser.add_argument('prompt', nargs='?', default=None, help='initial prompt')


def build_parser() -> Parser:
  parser = Parser(description='run a harness infused with a bro in a managed workspace')
  subparsers = parser.add_subparsers(dest='cmd', required=True)

  solo = subparsers.add_parser('solo', help='run a one-shot prompt and print the reply')
  _configure_mode_parser(solo, solo=True)

  along = subparsers.add_parser('along', help='start an interactive session')
  _configure_mode_parser(along, solo=False)

  resume = subparsers.add_parser(
    'resume', help='resume the last harness session in a workspace under its recorded recipe'
  )
  add_scope_flags(resume)
  resume.add_argument('name', help='workspace to resume, as `ride list` shows it')

  subparsers.add_parser('list', help='list workspaces ([.]=worktree, [o]=container, [x]=abandoned)')

  clean = subparsers.add_parser(
    'clean', help='remove stale workspaces and unlocked runtime bundles'
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
  scope.add_argument(
    '--repo', default=None, metavar='PATH|URL', help='attach an existing checkout path or git URL'
  )
  scope.add_argument('--bro', default=None, help='bro to scope (required when detached)')
  add_harness_flags(scope)

  banner_parser = subparsers.add_parser('banner', help='print the session banner')
  banner_parser.add_argument('--llm', action='store_true', help='emit plain key:value lines')
  return parser


def _resolve_repository_argument(value: str) -> Repository:
  if is_git_url(value) or Path(value).expanduser().exists():
    return resolve_repository(value)
  root = project_root(Path(value))
  return Repository(str(root), root)


def _parse_mode(parser: Parser, argv: list[str]) -> tuple[dict, list[str]]:
  try:
    separator = argv.index('--')
  except ValueError:
    return parser.parse(argv), []
  return parser.parse(argv[:separator]), argv[separator + 1 :]


def _parse(parser: Parser, argv: list[str]) -> tuple[dict, list[str]]:
  if len(argv) < 2 or argv[1] not in ('solo', 'along'):
    return parser.parse(argv), []
  return _parse_mode(parser, argv)


def _start_mode(parser: Parser, args: dict, harness_arguments: list[str], *, solo: bool) -> int:
  workspace = args.pop('workspace')
  in_place = args.pop('in_place')
  resume = args.pop('resume')
  summoned_token = args.pop('summoned', None)
  repo_argument = args.pop('repo')
  summoned = None if summoned_token is None else _peek_summoned(parser, summoned_token)
  if summoned is not None:
    if repo_argument is not None:
      parser.error('--summoned inherits its repository attachment; drop --repo')
    repo_argument = summoned.repo
  repository: Optional[Repository] = None
  if in_place or repo_argument is None:
    repo = repo_argument
  else:
    try:
      repository = _resolve_repository_argument(repo_argument)
    except (RuntimeError, ValueError) as error:
      parser.error(str(error))
    repo = repository.identity
  if solo:
    keep = args.pop('keep')
    if workspace is not None and keep:
      parser.error('--keep cannot be combined with --workspace; pinned workspaces are always kept')
    drop = not keep and workspace is None
  else:
    drop = args.pop('drop')
    keep = False
    if workspace is not None and drop:
      parser.error('--drop cannot be combined with --workspace; pinned workspaces are always kept')
  if resume and not in_place:
    parser.error('resuming is `ride resume <workspace>`; --resume is an inner-argv token')
  if in_place:
    machinery = {
      '--host': args['host'],
      '--drop': drop,
      '--keep': keep,
      '--grant': args['grant'] is not None,
      '--revoke': args['revoke'] is not None,
      '--into': args['into'] is not None,
    }
    offending = [flag for flag, present in machinery.items() if present]
    if len(offending) > 0:
      parser.error(f'--in-place cannot be combined with {", ".join(offending)}')
    if workspace is None:
      parser.error('--in-place requires --workspace')
  if args['into'] is not None and repo is None and summoned_token is None:
    parser.error('--into requires --repo')
  if args['hold'] is None:
    args['hold'] = default_hold(solo=solo, host=args['host'])
  config = (
    None
    if repository is None
    else (repository.project_config() if repository.is_url else project_config(repository.git_dir))
  )
  harness_name = args.pop('harness') or (config.harness if config is not None else 'claude')
  try:
    harness = get_harness(harness_name)
    canonicalize(args, selection_from_args(args, project=config))
    drop_piece_flags(args)
  except (LLMSelectionError, ValueError) as error:
    parser.error(str(error))
  args['grant'] = args['grant'] or []
  args['revoke'] = args['revoke'] or []
  bro = args.pop('bro')
  prompt = args.pop('prompt')
  if summoned is not None:
    _validate_summoned(parser, summoned, bro=bro, prompt=prompt, in_place=in_place, args=args)
    prompt = summoned.prompt
    args['grant'] = [*summoned.grant, *args['grant']]
    args['revoke'] = [*summoned.revoke, *args['revoke']]
  harness_options = pop_harness_options(parser, args, harness_name, solo=solo, host=args['host'])
  try:
    resolved_llm = harness.resolve_llm(args['llm'], bro)
  except (KeyError, LLMSelectionError, ValueError) as error:
    parser.error(str(error))
  name = workspace if workspace is not None else fresh_workspace_name(f'ride-{bro}')
  spec = SessionSpec(
    name=name,
    repo=repo,
    harness=harness_name,
    workspace_pinned=workspace is not None,
    drop=drop,
    bro=bro,
    prompt=prompt,
    subject=prompt,
    arguments=harness_arguments,
    resolved_llm=resolved_llm.dump(),
    solo=solo,
    resume=resume,
    harness_options=harness_options,
    **args,
  )
  if in_place:
    from ride.inner import run_in_place

    return run_in_place(harness, spec)
  return start_session(spec, repository, summoned=summoned)


def _peek_summoned(parser: Parser, token: str) -> pending_summon.PendingSummon:
  try:
    return pending_summon.peek(token)
  except (pending_summon.UnknownToken, ValueError) as error:
    parser.error(str(error))


def _validate_summoned(
  parser: Parser,
  pending: pending_summon.PendingSummon,
  *,
  bro: str,
  prompt: Optional[str],
  in_place: bool,
  args: dict,
) -> None:
  """validate a manual child launch against the request's fixed shape."""
  from ride.scope import split_scope_overrides

  if in_place:
    parser.error('--summoned is an outer launch flag; the inner run inherits it via the env')
  if prompt is not None:
    parser.error('--summoned takes its initial prompt from the summon request; drop the prompt')
  if args['into'] is not None:
    parser.error('--summoned takes its base from the summon request; drop --into')
  _, bro_overrides = split_scope_overrides([*args['grant'], *args['revoke']])
  if len(bro_overrides) > 0:
    parser.error(
      "a manual child's summon allow-list was fixed by the summon request; drop the "
      f'@bro override(s): {", ".join(sorted(bro_overrides))}'
    )
  if bro != pending.target:
    parser.error(f'the summon token names bro {pending.target!r}, not {bro!r}')


def alias_main(argv: list[str], *, solo: bool) -> int:
  parser = Parser(
    prog=Path(argv[0]).name,
    description='run a one-shot prompt and print the reply'
    if solo
    else 'start an interactive session',
  )
  _configure_mode_parser(parser, solo=solo)
  args, harness_arguments = _parse_mode(parser, argv)
  return _start_mode(parser, args, harness_arguments, solo=solo)


@reports_location_errors
def main(argv: list[str]) -> Optional[int]:
  parser = build_parser()
  args, harness_arguments = _parse(parser, argv)
  command = args.pop('cmd')
  if command not in ('solo', 'along') and len(harness_arguments) > 0:
    parser.error('`--` harness arguments are accepted only by `ride solo` and `ride along`')
  if command in ('solo', 'along'):
    return _start_mode(parser, args, harness_arguments, solo=command == 'solo')
  if command == 'list':
    return list_workspaces()
  if command == 'resume':
    return resume_session(args['name'], grant=args['grant'] or [], revoke=args['revoke'] or [])
  if command == 'clean':
    return clean_workspaces(force=args['force'], dry_run=args['dry_run'], names=args['names'])
  if command == 'check-clean':
    try:
      workspace = Workspace.open(args['name'])
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

    try:
      repository = None if args['repo'] is None else _resolve_repository_argument(args['repo'])
    except (RuntimeError, ValueError) as error:
      parser.error(str(error))
    if repository is None and args['bro'] is None:
      parser.error('ride scope requires --bro when detached')
    harness_name = args['harness'] or (
      (
        repository.project_config()
        if repository is not None and repository.is_url
        else project_config(None if repository is None else repository.git_dir)
      ).harness
      if repository is not None
      else 'claude'
    )
    options = pop_harness_options(parser, args, harness_name, solo=False, host=False)
    return report_scope(repo=repository, bro=args['bro'], harness=harness_name, options=options)
  assert command == 'banner'
  return banner(llm=args['llm'])
