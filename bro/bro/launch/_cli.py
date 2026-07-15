"""shared CLI plumbing for `ask` / `do-task` / `call`: the scoped-container hop,
fast-mode bro construction, and the `ask`/`do-task` main()."""

import asyncio
import logging
import os
import secrets
import sys
from collections.abc import Callable, Coroutine
from typing import Optional

import base.args
import summon as summon_client
from base import credentials, log
from bro.bro import BroRaised
from bro.bros.bro import Bro
from llm.llm import LLMSpec
from llm.observer import Observer

# shared flag help so all three CLIs describe `--slow` / `--host` identically.
# fast mode is the default for these CLIs; --slow opts back out to the plain spec.
SLOW_HELP = (
  "disable the bro's fast-mode LLM knob, which is on by default here "
  "(provider-specific; for ChatGPT fast mode is OpenAI's 'priority' service tier — "
  'same model and quality, faster and more consistent generation at a higher '
  'per-token price)'
)
EFFORT_HELP = (
  "override the reasoning-effort knob of the bro's LLM spec with this neutral level, "
  "mapped onto the provider's own scale (for ChatGPT, xhigh maps through and max caps "
  "at xhigh); without the flag the bro's own spec stands. errors when the provider "
  'has no effort knob'
)
HOST_HELP = (
  'run the bro in the calling process: skip the scoped-container hop (on the '
  'host) or the host-relayed full launch (inside a container)'
)
TIMEOUT_HELP = (
  'seconds before the host kills a relayed run — a container invocation without '
  f'--host relays through the host summon path (default: {summon_client.DEFAULT_TIMEOUT:.0f}); '
  'errors when the run is not relayed'
)
NO_TRAILS_HELP = (
  'disable trails recording: set TRAILS_DISABLED in the container and drop the '
  'trails secret from the scoped set'
)
GRANT_CRED_HELP = (
  "grant a secret to the container's scoped set on top of the bro's manifest "
  '(repeatable); errors if it is already in the set or unknown to the registry'
)
REVOKE_CRED_HELP = (
  "revoke a secret from the container's scoped set (repeatable); errors if it is not in the set"
)
GRANT_SUMMON_HELP = (
  'allow the bro to summon the named bro during this run, on top of its may_summon '
  'defaults (repeatable); errors if already allowed or not a registered bro'
)
REVOKE_SUMMON_HELP = (
  'disallow summoning the named bro during this run (repeatable); '
  'errors if it is not in the allow-list'
)
INTO_HELP = (
  "base the container's workspace clone on this git ref instead of the host "
  "checkout's current HEAD (fetched from origin when not local)"
)


def run_llm_spec(
  bro_class: type[Bro], *, fast: bool, effort: Optional[str] = None
) -> Optional[LLMSpec]:
  """the per-run LLM spec these CLIs run the bro with, or None when the class
  default stands. fast mode (the provider's fast knob) is the default; pass
  fast=False (`--slow`) for the plain spec. fast being implicit, a provider
  with no fast mode (e.g. echo) falls back to the normal spec rather than
  raising — the user never explicitly asked for fast. effort (`--effort`)
  overrides the spec's reasoning-effort knob via `LLMSpec.with_effort`; being
  an explicit ask, a provider without the knob raises NotImplementedError
  instead of falling back."""
  if not fast and effort is None:
    return None
  spec = bro_class.llm_spec
  if fast:
    try:
      spec = spec.fast()
    except NotImplementedError:
      logging.getLogger(__name__).debug(
        '%s has no fast mode; running with the normal spec', bro_class.__name__
      )
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
  bro_name: str,
  inner_args: list[str],
  host: bool,
  no_trails: bool = False,
  grant_cred: Optional[list[str]] = None,
  revoke_cred: Optional[list[str]] = None,
  grant_summon: Optional[list[str]] = None,
  revoke_summon: Optional[list[str]] = None,
  into: Optional[str] = None,
) -> Optional[int]:
  """re-exec `<cli_name> <bro_name> <inner_args...>` inside a scoped throwaway
  container and return its exit code, or return None so the caller runs in the
  calling process.

  the hop is skipped when `--host` was passed or when already inside a container
  (`CW_IN_CONTAINER`, set by the container). the hopped command itself carries
  `--host`, pinning the inner run in-process — without it an in-container
  `ask`/`do-task` would relay itself back to the host (the relay branch in
  `run()`); `call` has no relay, so its in-container invocations land here and
  skip via `CW_IN_CONTAINER`. otherwise the container is scoped
  to `cw.bro_run_secrets(bro_name)` — the LLM-process credential scope (see its
  docstring). an interactive surface (`call`) renders inside it just as claude
  code does.

  the container's workspace clone bases on the host checkout's current HEAD (the
  entrypoint's default, shared with `cw ss` — the bro sees the code the caller
  sees, minus uncommitted changes); `into` (`--into <ref>`) bases it on any
  branch/tag/sha instead, resolved with an origin fetch when the ref isn't
  local, and an unresolvable explicit ref fails fast.

  `no_trails` drops `trails` from the scoped set and sets `TRAILS_DISABLED` in the
  container (the in-container tracker factory then returns `NullTracker`).

  `grant_cred`/`revoke_cred` adjust that scoped set per
  `credentials.apply_grant_revoke` (strict: see its rules); `grant_summon`/
  `revoke_summon` adjust the bro's summon allow-list the same way
  (`cw.summon_allow_list` over its `may_summon` defaults). those four and `into`
  are host-side only — not threaded into the inner command — so passing any when
  the hop is skipped (`--host` / already in-container) is a no-op the
  caller didn't get, hence an error: host mode is unscoped, has no broker root,
  and runs no clone. returns 1 (printing to stderr) on any misuse so the caller
  exits non-zero."""
  grant_cred = grant_cred if grant_cred is not None else []
  revoke_cred = revoke_cred if revoke_cred is not None else []
  grant_summon = grant_summon if grant_summon is not None else []
  revoke_summon = revoke_summon if revoke_summon is not None else []
  if host or os.environ.get('CW_IN_CONTAINER') is not None:
    if (
      len(grant_cred) > 0
      or len(revoke_cred) > 0
      or len(grant_summon) > 0
      or len(revoke_summon) > 0
      or into is not None
    ):
      print(
        '--grant-cred/--revoke-cred/--grant-summon/--revoke-summon/--into require '
        'containerization (not valid with --host)',
        file=sys.stderr,
      )
      return 1
    return None
  from cw import (
    _project_root,
    bro_git_identity_env,
    bro_run_secrets,
    resolve_ref,
    run_in_container,
    summon_allow_list,
  )

  scoped = bro_run_secrets(bro_name)
  needed = set(scoped.required)
  # every cw-launched session commits as bro; a native bro run gets no in-place
  # session runner to export the identity, so the hop sets it in the container
  extra_env: dict[str, str] = dict(bro_git_identity_env())
  if no_trails:
    needed.remove('trails')
    extra_env['TRAILS_DISABLED'] = '1'
  try:
    needed = credentials.apply_grant_revoke(
      needed, grant=grant_cred, revoke=revoke_cred, subject='scoped credential set'
    )
    may_summon = summon_allow_list(bro_name, grant=grant_summon, revoke=revoke_summon)
  except ValueError as e:
    print(str(e), file=sys.stderr)
    return 1
  if into is not None:
    base_ref = resolve_ref(_project_root(), into)
    if base_ref is None:
      print(f'cannot resolve --into ref: {into}', file=sys.stderr)
      return 1
    extra_env['CW_BASE_REF'] = base_ref
  workspace = f'{cli_name}-{bro_name}-{secrets.token_hex(4)}'
  command = [cli_name, bro_name, *inner_args, '--host']
  return run_in_container(
    workspace,
    command,
    drop=True,
    secrets=needed,
    optional_secrets=scoped.optional,
    docker_sock=scoped.docker_sock,
    extra_env=extra_env,
    # this container runs its own named bro, so the calling session's ambient
    # CW_BRO must not leak in and mis-theme it.
    forward_bro=False,
    may_summon=may_summon,
  )


def _relay_blocked_flags(args: dict) -> list[str]:
  """the flags a host-relayed run cannot honor: the host launches the target
  with its own defaults (`ask <target> "<prompt>"`), so local trace rendering
  and per-run spec/scope adjustments have nothing to act on."""
  blocked: list[str] = []
  if args['rich']:
    blocked.append('--rich')
  if args['slow']:
    blocked.append('--slow')
  if args['effort'] is not None:
    blocked.append('--effort')
  if args['no_trails']:
    blocked.append('--no-trails')
  if args['grant_cred'] is not None:
    blocked.append('--grant-cred')
  if args['revoke_cred'] is not None:
    blocked.append('--revoke-cred')
  if args['grant_summon'] is not None:
    blocked.append('--grant-summon')
  if args['revoke_summon'] is not None:
    blocked.append('--revoke-summon')
  return blocked


def _relay_through_host(
  *, bro_name: str, prompt: str, timeout: Optional[float], into: Optional[str]
) -> int:
  """run the bro through the host's summon path — the in-container counterpart
  of the container hop, so the run gets the full launch flow instead of an
  in-process run against this container's credentials: the host spawns a sibling
  container with the target's own scoped credential set, git identity, trails
  recording, and workspace clone, subject to the session's summon allow-list and
  depth cap. blocks and relays the answer (`summon.relay_summon`)."""
  if os.environ.get('BROKER_CHANNEL') is None:
    print(
      'no broker channel to relay through; pass --host to run the bro in this process',
      file=sys.stderr,
    )
    return 1
  log.info('inside a container: relaying to the host for a scoped %s run', bro_name)
  return summon_client.relay_summon(bro_name, prompt, timeout=timeout, into=into)


def run(
  *,
  cli_name: str,
  parser_description: str,
  arg_name: str,
  arg_help: str,
  run_function: Callable[[Bro, str, Optional[Observer]], Coroutine[None, None, str]],
  argv: list[str],
  export_task_id: bool = False,
  relay_prompt: Optional[Callable[[str], str]] = None,
) -> Optional[int]:
  """shared `ask`/`do-task` main. `export_task_id` (do-task) parses the task
  positional as a Notion page ref and exports it as CW_TASK_ID, so the run's
  commit footer resolves its `Task:` line without a flow lookup; input that is
  no page ref (a description, a slash invocation) exports nothing. a host-relayed
  run sends `relay_prompt(<positional>)` as the summon prompt (do-task's `/fix`
  wrapping — the relayed child runs plain `ask`); None sends it verbatim."""
  from cw import EFFORT_LEVELS

  parser = base.args.Parser(description=parser_description)
  parser.add_argument('bro', help='bro name')
  parser.add_argument(arg_name, help=arg_help)
  parser.add_argument(
    '--rich',
    action='store_true',
    help='render the trace as colored rich panels instead of plain log lines',
  )
  parser.add_argument('--slow', action='store_true', help=SLOW_HELP)
  parser.add_argument('--effort', choices=EFFORT_LEVELS, default=None, help=EFFORT_HELP)
  parser.add_argument('--host', action='store_true', help=HOST_HELP)
  parser.add_argument('--no-trails', dest='no_trails', action='store_true', help=NO_TRAILS_HELP)
  # --no-trails acts only on the container hop; --host has no hop to act on.
  parser.add_exclusive_groups(['host'], ['no_trails'])
  parser.add_argument(
    '--grant-cred', action='append', default=None, metavar='SECRET', help=GRANT_CRED_HELP
  )
  parser.add_argument(
    '--revoke-cred', action='append', default=None, metavar='SECRET', help=REVOKE_CRED_HELP
  )
  parser.add_argument(
    '--grant-summon', action='append', default=None, metavar='BRO', help=GRANT_SUMMON_HELP
  )
  parser.add_argument(
    '--revoke-summon', action='append', default=None, metavar='BRO', help=REVOKE_SUMMON_HELP
  )
  parser.add_argument('--into', metavar='REF', help=INTO_HELP)
  parser.add_argument('--timeout', type=float, metavar='SECONDS', help=TIMEOUT_HELP)
  args = parser.parse(argv)

  if export_task_id:
    from notion import parse_page_ref

    try:
      # before the hop: the container create captures CW_TASK_ID from this
      # process's environment; on the hop-less paths it stays for the bro's
      # tool subprocesses.
      os.environ['CW_TASK_ID'] = parse_page_ref(args[arg_name])
    except ValueError:
      pass

  if os.environ.get('CW_IN_CONTAINER') is not None and not args['host']:
    blocked = _relay_blocked_flags(args)
    if len(blocked) > 0:
      print(
        f'{"/".join(blocked)} cannot ride a host-relayed run (the host launches '
        f'{args["bro"]} with its own defaults); pass --host to run in this process',
        file=sys.stderr,
      )
      return 1
    prompt = args[arg_name] if relay_prompt is None else relay_prompt(args[arg_name])
    return _relay_through_host(
      bro_name=args['bro'], prompt=prompt, timeout=args['timeout'], into=args['into']
    )
  if args['timeout'] is not None:
    print(
      '--timeout only bounds a host-relayed run (a container invocation without --host)',
      file=sys.stderr,
    )
    return 1

  inner_args = [args[arg_name]]
  if args['rich']:
    inner_args.append('--rich')
  if args['slow']:
    inner_args.append('--slow')
  if args['effort'] is not None:
    inner_args.extend(['--effort', args['effort']])
  hopped = maybe_containerize(
    cli_name=cli_name,
    bro_name=args['bro'],
    inner_args=inner_args,
    host=args['host'],
    no_trails=args['no_trails'],
    grant_cred=args['grant_cred'],
    revoke_cred=args['revoke_cred'],
    grant_summon=args['grant_summon'],
    revoke_summon=args['revoke_summon'],
    into=args['into'],
  )
  if hopped is not None:
    return hopped

  try:
    bro = create_bro_for_run(args['bro'], fast=not args['slow'], effort=args['effort'])
  except NotImplementedError as e:
    # --effort on a provider without the knob — an explicit ask, so a clean
    # error instead of fast mode's silent fallback.
    print(str(e), file=sys.stderr)
    return 1
  observer: Optional[Observer] = None
  if args['rich']:
    from llm.observer import RichConsoleRenderer

    observer = RichConsoleRenderer(prefix=bro.name)
  try:
    result = asyncio.run(run_function(bro, args[arg_name], observer))
  except BroRaised as e:
    print(f'raised: {e.reason}', file=sys.stderr)
    return 1
  print(result)
