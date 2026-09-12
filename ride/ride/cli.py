#!/usr/bin/env python
import sys
from pathlib import Path
from typing import Optional

from bro.base import configs, host_config
from bro.base.args import REMAINDER, Parser
from bro.launch.llm_flags import canonicalize, drop_piece_flags, selection_from_args
from bro.llm.providers import LLMSelectionError
from bro.registry import get_class
from bro.workspace.banner import banner
from bro.workspace.paths import fresh_workspace_name, project_root
from bro.workspace.project import project_config
from ride import pending_summon
from ride.clean import clean_workspaces
from ride.errors import reports_runtime_errors
from ride.flags import (
  add_harness_flags,
  add_scope_flags,
  add_session_flags,
  default_hold,
  isolation_from_args,
  pop_harness_options,
)
from ride.harness import get_harness
from ride.listing import list_workspaces
from ride.repository import Repository, is_git_url, resolve_repository
from ride.runtime_bundle import reexec_from_runtime
from ride.runtime_state import migrate_runtime_state
from ride.session import SessionSpec, recorded_runtime_reference, resume_session, start_session
from ride.workspace.containers import exec_in_workspace
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace

__cli_name__ = 'ride'


def _add_mode_flags(parser: Parser) -> None:
  parser.add_argument(
    '--repo',
    default=None,
    metavar='PATH|URL',
    help='attach an existing checkout path or git URL (default: detached)',
  )
  parser.add_argument(
    '--tree',
    default=None,
    metavar='PATH',
    help='run a detached unboxed session in an existing directory without owning it',
  )
  parser.add_argument(
    '--runtime-bundle',
    default=None,
    metavar='PATH',
    help='run from a materialized runtime at PATH (PATH/venv and PATH/bin)',
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

  subparsers.add_parser('list', help='list workspaces ([o]/[-]=boxed, .o./.-.=unboxed; live/idle)')

  clean = subparsers.add_parser(
    'clean', help='remove stale workspaces, unreferenced mirrors, and unlocked runtime bundles'
  )
  clean.add_argument(
    '--force',
    action='store_true',
    help='remove workspaces even when dirty or their repository is unavailable',
  )
  clean.add_argument('--dry-run', action='store_true', help='show removals without applying them')
  clean.add_argument('names', nargs='*', help='workspaces to clean (default: all)')

  check_clean = subparsers.add_parser(
    'check-clean', help='check whether a workspace is safe to remove (exit 0=yes, 1=no)'
  )
  check_clean.add_argument('name', help='workspace to check')

  exec_command = subparsers.add_parser(
    'exec', help='exec a command in a running boxed workspace (default: interactive bash)'
  )
  exec_command.add_argument('name', help='boxed workspace name')
  exec_command.add_argument('command', nargs=REMAINDER, help='command and arguments')

  scope = subparsers.add_parser(
    'scope', help='print a prospective session credential scope with instance-selection layers'
  )
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


def _bootstrap_mode_runtime(parser: Parser, args: dict, launch_argv: list[str]) -> None:
  token = args.get('summoned')
  runtime = args.get('runtime_bundle')
  if token is not None:
    if runtime is not None:
      parser.error('--summoned takes its runtime from the summon token; drop --runtime-bundle')
    try:
      runtime = pending_summon.runtime_reference(token)
    except (pending_summon.UnknownToken, ValueError) as error:
      parser.error(str(error))
  elif runtime is not None:
    runtime = str(Path(runtime).expanduser().resolve())
  if runtime is None:
    return
  try:
    reexec_from_runtime(runtime, launch_argv)
  except RuntimeError as error:
    parser.error(str(error))
  args['runtime_bundle'] = runtime


def _start_mode(
  parser: Parser,
  args: dict,
  harness_arguments: list[str],
  *,
  solo: bool,
) -> int:
  workspace = args.pop('workspace')
  summoned_token = args.pop('summoned', None)
  repo_argument = args.pop('repo')
  tree_argument = args.pop('tree')
  runtime_argument = args.pop('runtime_bundle')
  summoned = None if summoned_token is None else _peek_summoned(parser, summoned_token)
  if summoned is not None:
    if repo_argument is not None:
      parser.error('--summoned inherits its repository attachment; drop --repo')
    repo_argument = summoned.repo
  repository: Optional[Repository] = None
  if repo_argument is None:
    repo = None
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
  if args['into'] is not None and repo is None and summoned_token is None:
    parser.error('--into requires --repo')
  isolation = isolation_from_args(args)
  tree = None if tree_argument is None else str(Path(tree_argument).expanduser().resolve())
  if tree is not None:
    if repo is not None:
      parser.error('--tree cannot be combined with --repo')
    if isolation is not Isolation.UNBOXED:
      parser.error('--tree requires --unboxed')
  runtime_bundle = runtime_argument
  if args['hold'] is None:
    args['hold'] = default_hold(solo=solo, isolation=isolation)
  config = (
    None
    if repository is None
    else (repository.project_config() if repository.is_url else project_config(repository.git_dir))
  )
  harness_name = args.pop('harness') or (config.harness if config is not None else 'claude')
  try:
    harness = get_harness(harness_name)
    canonicalize(args, selection_from_args(args, project=config))
    summon_depth = host_config.summon_depth(None if config is None else config.summon_depth)
    summon_harness = config.summon_harness if config is not None else configs.DEFAULT_SUMMON_HARNESS
    drop_piece_flags(args)
  except (LLMSelectionError, ValueError) as error:
    parser.error(str(error))
  args['grant'] = args['grant'] or []
  args['revoke'] = args['revoke'] or []
  bro = args.pop('bro')
  prompt = args.pop('prompt')
  if summoned is not None:
    _validate_summoned(parser, summoned, bro=bro, prompt=prompt, args=args)
    prompt = summoned.prompt
    args['grant'] = [*summoned.grant, *args['grant']]
    args['revoke'] = [*summoned.revoke, *args['revoke']]
  harness_options = pop_harness_options(parser, args, harness_name, solo=solo, isolation=isolation)
  try:
    # not every harness's llm resolution consults the registry, so the launch
    # checks the name itself
    get_class(bro)
    resolved_llm = harness.resolve_llm(args['llm'], bro)
  except (KeyError, LLMSelectionError, ValueError) as error:
    parser.error(str(error))
  name = workspace if workspace is not None else fresh_workspace_name(f'ride-{bro}')
  spec = SessionSpec(
    name=name,
    repo=repo,
    harness=harness_name,
    workspace_pinned=workspace is not None,
    isolation=isolation,
    drop=drop,
    bro=bro,
    prompt=prompt,
    subject=prompt,
    arguments=harness_arguments,
    resolved_llm=resolved_llm.dump(),
    solo=solo,
    resume=False,
    harness_options=harness_options,
    summon_depth=summon_depth,
    summon_harness=summon_harness,
    tree=tree,
    runtime_bundle=runtime_bundle,
    **args,
  )
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
  args: dict,
) -> None:
  """validate a manual child launch against the request's fixed shape."""
  from ride.scope import split_scope_overrides

  if prompt is not None:
    parser.error('--summoned takes its initial prompt from the summon request; drop the prompt')
  if args['into'] is not None:
    parser.error('--summoned takes its base from the summon request; drop --into')
  _, bro_overrides, permit_overrides = split_scope_overrides([*args['grant'], *args['revoke']])
  if len(bro_overrides) > 0:
    parser.error(
      "a manual child's summon allow-list was fixed by the summon request; drop the "
      f'@bro override(s): {", ".join(sorted(bro_overrides))}'
    )
  if len(permit_overrides) > 0:
    parser.error(
      "a manual child's permits were fixed by the summon request; drop the "
      f':permit override(s): {", ".join(sorted(permit_overrides))}'
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
  launch_argv = ['ride', 'solo' if solo else 'along', *argv[1:]]
  _bootstrap_mode_runtime(parser, args, launch_argv)
  migrate_runtime_state()
  return _start_mode(parser, args, harness_arguments, solo=solo)


@reports_runtime_errors
def main(argv: list[str]) -> Optional[int]:
  parser = build_parser()
  args, harness_arguments = _parse(parser, argv)
  command = args.pop('cmd')
  if command in ('solo', 'along'):
    _bootstrap_mode_runtime(parser, args, argv)
  elif command == 'resume':
    try:
      runtime = recorded_runtime_reference(args['name'])
      if runtime is not None:
        reexec_from_runtime(runtime, argv)
    except (ValueError, RuntimeError) as error:
      parser.error(str(error))
  migrate_runtime_state()
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
    options = pop_harness_options(parser, args, harness_name, solo=False, isolation=Isolation.BOXED)
    return report_scope(repo=repository, bro=args['bro'], harness=harness_name, options=options)
  assert command == 'banner'
  return banner(llm=args['llm'])
