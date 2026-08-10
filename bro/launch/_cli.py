"""shared bro launcher plumbing: container hops, one-shot runs, and bro construction."""

import asyncio
import os
from typing import Literal, Optional

import bro.base.args as base_args
import bro.launch.bro_run
from bro import summon as summon_client
from bro.base import log
from bro.bro import BroRaised
from bro.bros.bro import Bro
from bro.llm.llm import EFFORT_LEVELS, LLMSpec
from bro.llm.mcp import HOLDS
from bro.llm.observer import Observer

# shared flag help so all the launcher CLIs describe `--fast` / `--in-place` identically.
FAST_HELP = (
  "run with the bro's fast-mode LLM knob (provider-specific; for ChatGPT fast mode is "
  "OpenAI's 'priority' service tier — same model and quality, faster and more consistent "
  'generation at a higher per-token price); implied by the ask and call aliases; a provider '
  'with no fast mode falls back to the plain spec'
)
EFFORT_HELP = (
  "override the reasoning-effort knob of the bro's LLM spec with this neutral level, "
  "mapped onto the provider's own scale (for ChatGPT, xhigh maps through and max caps "
  "at xhigh); without the flag the bro's own spec stands. errors when the provider "
  'has no effort knob'
)
IN_PLACE_HELP = 'run the bro in the calling process instead of creating an isolated container'
HOLD_HELP = (
  "the run's hold — the user-involvement level whose fragment lands in the system prompt "
  '(unattended = no human channel, detached = launched and left, attended = human watching, '
  'guided = human drives each step); default: {}'
)
SUMMON_HELP = 'run through the session summon channel in a separate scoped container'
TIMEOUT_HELP = (
  f'summon mode: seconds before the host kills the child (default: '
  f'{summon_client.DEFAULT_TIMEOUT:.0f})'
)
DETACH_HELP = (
  'summon mode: print the request id and exit after sending; collect it with summon check'
)
NO_TRAILS_HELP = (
  'disable trails recording: set TRAILS_DISABLED in the container and drop the '
  'trails secret from the scoped set'
)
GRANT_HELP = (
  "add a credential (NAME) or a summonable bro (@BRO) to the run's scope on top of the "
  "bro's manifest and may_summon defaults; a credential grant replaces the selected "
  'same-kind name (repeatable); errors on an exact duplicate or unknown name'
)
REVOKE_HELP = (
  "remove a credential (NAME) or a summonable bro (@BRO) from the run's scope "
  '(repeatable); errors if it is not in the scope'
)
INTO_HELP = (
  "base the new workspace clone on this git ref instead of the launcher's current HEAD "
  '(fetched from origin when not local)'
)


def run_llm_spec(
  bro_class: type[Bro], *, fast: bool, effort: Optional[str] = None
) -> Optional[LLMSpec]:
  """the per-run LLM spec these CLIs run the bro with, or None when the class
  default stands. fast (`--fast`, implied by the `ask` / `call` aliases) applies
  the provider's fast knob; a provider with no fast mode (e.g. echo) falls back
  to the normal spec rather than raising — the aliases imply fast without the
  user asking for it. effort (`--effort`) overrides the spec's reasoning-effort
  knob via `LLMSpec.with_effort`; being an explicit ask, a provider without the
  knob raises NotImplementedError instead of falling back."""
  if not fast and effort is None:
    return None
  spec = bro_class.llm_spec
  if fast:
    try:
      spec = spec.fast()
    except NotImplementedError:
      log.verbose('%s has no fast mode; running with the normal spec', bro_class.__name__)
  if effort is not None:
    spec = spec.with_effort(effort)
  return None if spec is bro_class.llm_spec else spec


def create_bro_for_run(bro_name: str, *, fast: bool, effort: Optional[str] = None) -> Bro:
  """instantiate the bro for an in-process run, with the `run_llm_spec`
  override applied when one is needed."""
  from bro.registry import create_bro, get_class

  if not fast and effort is None:
    return create_bro(bro_name)
  bro_class = get_class(bro_name)
  spec = run_llm_spec(bro_class, fast=fast, effort=effort)
  if spec is None:
    return create_bro(bro_name)
  return bro_class.create(spec)


def maybe_containerize(
  *,
  cli_name: str,
  verb: Literal['run', 'chat'],
  bro_name: str,
  inner_args: list[str],
  in_place: bool,
  no_trails: bool = False,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  into: Optional[str] = None,
) -> Optional[int]:
  """re-exec `bro <verb> <bro_name> <inner_args...>` inside a scoped throwaway
  container and return its exit code, or return None so the caller runs in the
  calling process. `cli_name` is the outer spelling the user invoked; it only
  names the bro.workspace.the hop is skipped when `--in-place` was passed or when already inside a container
  (`CW_IN_CONTAINER`, set by the container). Callers reject an implicit in-container
  run before reaching this helper; the hopped command carries `--in-place`, pinning
  the already-scoped inner run in-process. Otherwise the launch is the shared bro-run
  description (`bro.launch.bro_run.describe`): a fresh workspace, the bro's own credential
  scope, the bro git identity + `CW_BRO` in the container env. an interactive
  surface (`bro chat`) renders inside it just as claude code does.

  the container's workspace clone bases on the host checkout's current HEAD (the
  entrypoint's default, shared with `cw ss` — the bro sees the code the caller
  sees, minus uncommitted changes); `into` (`--into <ref>`) bases it on any
  branch/tag/sha instead, resolved with an origin fetch when the ref isn't
  local, and an unresolvable explicit ref fails fast.

  `no_trails` drops `trails` from the scoped set and sets `TRAILS_DISABLED` in the
  container (the in-container tracker factory then returns `NullTracker`).

  `grant`/`revoke` adjust the run's launch scope — a plain name a credential
  across both tiers of the scoped set, `@bro` the summon allow-list over the
  bro's `may_summon` defaults — both strict, applied by the launch-scope
  preflight (`bro.launch.scope.preflight_scoped_launch`). those two and `into`
  are host-side only — not threaded into the inner command — so passing any
  when the hop is skipped (`--in-place` / already in-container) is a no-op the
  caller didn't get, hence an error: the in-place path creates no credential
  scope or broker root and runs no clone. returns 1 (printing to stderr) on any
  misuse so the caller exits non-zero."""
  grant = grant if grant is not None else []
  revoke = revoke if revoke is not None else []
  if in_place or os.environ.get('CW_IN_CONTAINER') is not None:
    if len(grant) > 0 or len(revoke) > 0 or into is not None:
      log.error('--grant/--revoke/--into require containerization (not valid with --in-place)')
      return 1
    return None
  from bro.launch.root import run_in_container
  from bro.launch.scope import (
    LaunchScopeError,
    Surface,
    preflight_scoped_launch,
    scoped_secrets,
  )
  from bro.workspace.git import resolve_ref
  from bro.workspace.paths import fresh_workspace_name, project_root
  from bro.workspace.project import project_config

  base_ref: Optional[str] = None
  if into is not None:
    base_ref = resolve_ref(project_root(), into)
    if base_ref is None:
      log.error('cannot resolve --into ref: %s', into)
      return 1
  try:
    scoped, may_summon, _ = preflight_scoped_launch(
      scoped_secrets(bro_name, Surface.BRO_RUN, credential_instances=project_config().creds),
      bro_name,
      grant=grant,
      revoke=revoke,
    )
  except LaunchScopeError as e:
    log.error('%s', e)
    return 1
  launch = bro.launch.bro_run.describe(
    bro_name,
    inner_args,
    workspace_name=fresh_workspace_name(f'{cli_name}-{bro_name}'),
    verb=verb,
    scoped=scoped,
    base_ref=base_ref,
    trails=not no_trails,
  )
  return run_in_container(launch, drop=True, may_summon=may_summon)


def _run_summoned(
  bro_name: str,
  input_text: str,
  *,
  timeout: Optional[float],
  into: Optional[str],
  detach: bool,
  hold: Optional[str],
  grant: Optional[list[str]],
  revoke: Optional[list[str]],
  effort: Optional[str],
  fast: bool,
) -> int:
  if not detach:
    return summon_client.relay_summon(
      bro_name,
      input_text,
      timeout=timeout,
      into=into,
      hold=hold,
      grant=grant,
      revoke=revoke,
      effort=effort,
      fast=fast,
    )
  try:
    request_id = summon_client.summon_detached(
      bro_name,
      input_text,
      timeout=timeout,
      into=into,
      hold=hold,
      grant=grant,
      revoke=revoke,
      effort=effort,
      fast=fast,
    )
  except summon_client.SummonError as error:
    log.error('%s', error)
    return 1
  print(request_id)
  return 0


def run_main(
  argv: list[str],
  *,
  program: list[str],
  description: str = 'run a bro on the given input',
  force_summon: bool = False,
  implied_fast: bool = False,
) -> Optional[int]:
  """run the canonical one-shot launcher under `program`.

  aliases share this parser and execution path. `force_summon` supplies bare summon's
  implicit mode, and `implied_fast` supplies the ask alias's fast default.
  """
  parser = base_args.Parser(prog=' '.join(program), description=description)
  if force_summon:
    parser.add_argument('bro', metavar='target', help='bro to summon')
    parser.add_argument('input', metavar='prompt', help='request the summoned bro answers')
  else:
    parser.add_argument('bro', help='bro name')
    parser.add_argument('input', help='input to send to the bro')
  if not force_summon:
    parser.add_argument(
      '--rich',
      action='store_true',
      help='render the trace as colored rich panels instead of plain log lines',
    )
  parser.add_argument('--fast', action='store_true', help=FAST_HELP)
  parser.add_argument('--effort', choices=EFFORT_LEVELS, default=None, help=EFFORT_HELP)
  if not force_summon:
    parser.add_argument('--summon', action='store_true', help=SUMMON_HELP)
    parser.add_argument('--in-place', action='store_true', help=IN_PLACE_HELP)
    parser.add_argument('--no-trails', dest='no_trails', action='store_true', help=NO_TRAILS_HELP)
    parser.add_exclusive_groups(['in_place'], ['no_trails'])
    parser.add_exclusive_groups(['summon'], ['rich', 'in_place', 'no_trails'])
  parser.add_argument('--grant', action='append', default=None, metavar='NAME', help=GRANT_HELP)
  parser.add_argument('--revoke', action='append', default=None, metavar='NAME', help=REVOKE_HELP)
  parser.add_argument('--into', metavar='REF', help=INTO_HELP)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP.format('unattended'))
  parser.add_argument('--timeout', type=float, metavar='SECONDS', help=TIMEOUT_HELP)
  parser.add_argument('--detach', action='store_true', help=DETACH_HELP)

  args = parser.parse(argv)
  if force_summon:
    args.update(summon=True, rich=False, in_place=False, no_trails=False)
  shell_command = parser.reconstruct(args, prog=program)
  os.environ.setdefault('BRO_SHELL_COMMAND', ' '.join(shell_command))

  input_text = args['input']
  fast = args['fast'] or implied_fast

  if args['summon']:
    return _run_summoned(
      args['bro'],
      input_text,
      timeout=args['timeout'],
      into=args['into'],
      detach=args['detach'],
      hold=args['hold'],
      grant=args['grant'],
      revoke=args['revoke'],
      effort=args['effort'],
      fast=fast,
    )
  if args['timeout'] is not None or args['detach']:
    log.error('--timeout/--detach require --summon')
    return 1
  if os.environ.get('CW_IN_CONTAINER') is not None and not args['in_place']:
    log.error(
      'bro run refuses an implicit in-container run; pass --summon for an isolated run '
      "or --in-place to use this container's scope"
    )
    return 1

  inner_args = [input_text]
  if args['rich']:
    inner_args.append('--rich')
  if fast:
    inner_args.append('--fast')
  if args['effort'] is not None:
    inner_args.extend(['--effort', args['effort']])
  if args['hold'] is not None:
    inner_args.extend(['--hold', args['hold']])
  hopped = maybe_containerize(
    cli_name='bro-run' if program == ['bro', 'run'] else program[0],
    verb='run',
    bro_name=args['bro'],
    inner_args=inner_args,
    in_place=args['in_place'],
    no_trails=args['no_trails'],
    grant=args['grant'],
    revoke=args['revoke'],
    into=args['into'],
  )
  if hopped is not None:
    return hopped

  log.verbose('creating bro %s', args['bro'])
  try:
    bro = create_bro_for_run(args['bro'], fast=fast, effort=args['effort'])
  except NotImplementedError as error:
    log.error('%s', error)
    return 1
  observer: Optional[Observer] = None
  if args['rich']:
    from bro.llm.observer import RichConsoleRenderer

    observer = RichConsoleRenderer(prefix=bro.name)
  hold = args['hold'] if args['hold'] is not None else 'unattended'
  try:
    result = asyncio.run(bro.run(input_text, observer=observer, surface='ask', hold=hold))
  except BroRaised as error:
    log.error('raised: %s', error.reason)
    return 1
  except KeyboardInterrupt:
    # Ctrl+C cancels the run through the loop; the shell asked for this, so it
    # reads as an interrupted command, not a crashed one
    log.error('interrupted')
    return 130
  print(result)
