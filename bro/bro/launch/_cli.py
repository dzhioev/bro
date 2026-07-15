"""shared bro launcher plumbing: container hops, one-shot runs, and bro construction."""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from typing import Optional

import base.args
import cw.bro_run
import summon as summon_client
from base import credentials
from bro.bro import BroRaised
from bro.bros.bro import Bro
from llm.llm import LLMSpec
from llm.observer import Observer

# shared flag help so all three CLIs describe `--slow` / `--in-place` identically.
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
IN_PLACE_HELP = 'run the bro in the calling process instead of creating an isolated container'
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
GRANT_CRED_HELP = (
  "grant a secret to the container's scoped set on top of the bro's manifest "
  '(repeatable); errors if it is already in the set or unknown to the registry'
)
REVOKE_CRED_HELP = (
  "revoke a required or optional secret from the container's scoped set (repeatable); "
  'errors if it is not in either tier'
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
  "base the new workspace clone on this git ref instead of the launcher's current HEAD "
  '(fetched from origin when not local)'
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
  inner_cli_name: Optional[str] = None,
  in_place: bool,
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

  the hop is skipped when `--in-place` was passed or when already inside a container
  (`CW_IN_CONTAINER`, set by the container). Callers reject an implicit in-container
  run before reaching this helper; the hopped command carries `--in-place`, pinning
  the already-scoped inner run in-process. Otherwise the launch is the shared bro-run
  description (`cw.bro_run.describe`): a fresh workspace, the bro's own credential
  scope, the bro git identity + `CW_BRO` in the container env. an interactive
  surface (`call`) renders inside it just as claude code does.

  the container's workspace clone bases on the host checkout's current HEAD (the
  entrypoint's default, shared with `cw ss` — the bro sees the code the caller
  sees, minus uncommitted changes); `into` (`--into <ref>`) bases it on any
  branch/tag/sha instead, resolved with an origin fetch when the ref isn't
  local, and an unresolvable explicit ref fails fast.

  `no_trails` drops `trails` from the scoped set and sets `TRAILS_DISABLED` in the
  container (the in-container tracker factory then returns `NullTracker`).

  `grant_cred`/`revoke_cred` adjust both tiers of that scoped set per
  `cw.finalize_scoped_secrets` (strict: see its rules); `grant_summon`/
  `revoke_summon` adjust the bro's summon allow-list the same way
  (`cw.summon_allow_list` over its `may_summon` defaults). those four and `into`
  are host-side only — not threaded into the inner command — so passing any when
  the hop is skipped (`--in-place` / already in-container) is a no-op the
  caller didn't get, hence an error: the in-place path creates no credential scope or
  broker root and runs no clone. returns 1 (printing to stderr) on any misuse so the
  caller exits non-zero."""
  grant_cred = grant_cred if grant_cred is not None else []
  revoke_cred = revoke_cred if revoke_cred is not None else []
  grant_summon = grant_summon if grant_summon is not None else []
  revoke_summon = revoke_summon if revoke_summon is not None else []
  if in_place or os.environ.get('CW_IN_CONTAINER') is not None:
    if (
      len(grant_cred) > 0
      or len(revoke_cred) > 0
      or len(grant_summon) > 0
      or len(revoke_summon) > 0
      or into is not None
    ):
      print(
        '--grant-cred/--revoke-cred/--grant-summon/--revoke-summon/--into require '
        'containerization (not valid with --in-place)',
        file=sys.stderr,
      )
      return 1
    return None
  from cw import (
    ScopedSecrets,
    _project_root,
    finalize_scoped_secrets,
    fresh_workspace_name,
    resolve_ref,
    run_in_container,
    summon_allow_list,
  )

  base_ref: Optional[str] = None
  if into is not None:
    base_ref = resolve_ref(_project_root(), into)
    if base_ref is None:
      print(f'cannot resolve --into ref: {into}', file=sys.stderr)
      return 1
  launch = cw.bro_run.describe(
    bro_name,
    inner_args,
    workspace_name=fresh_workspace_name(f'{cli_name}-{bro_name}'),
    cli_name=inner_cli_name if inner_cli_name is not None else cli_name,
    base_ref=base_ref,
    trails=not no_trails,
  )
  try:
    scoped = finalize_scoped_secrets(
      ScopedSecrets(set(launch.secrets), set(launch.optional_secrets), launch.docker_sock),
      grant=grant_cred,
      revoke=revoke_cred,
    )
    may_summon = summon_allow_list(bro_name, grant=grant_summon, revoke=revoke_summon)
    # the container launch path repeats this build before create; the preflight
    # keeps missing-secret failures on the invoking CLI's error surface.
    credentials.build_scoped_store(scoped.required, optional=scoped.optional)
  except (ValueError, credentials.SecretNotFound) as e:
    print(str(e), file=sys.stderr)
    return 1
  launch = replace(launch, secrets=scoped.required, optional_secrets=scoped.optional)
  return run_in_container(launch, drop=True, may_summon=may_summon)


def _run_summoned(
  bro_name: str,
  input_text: str,
  *,
  timeout: Optional[float],
  into: Optional[str],
  detach: bool,
) -> int:
  if not detach:
    return summon_client.relay_summon(bro_name, input_text, timeout=timeout, into=into)
  try:
    request_id = summon_client.summon_detached(bro_name, input_text, timeout=timeout, into=into)
  except summon_client.SummonError as error:
    print(str(error), file=sys.stderr)
    return 1
  print(request_id)
  return 0


def run_main(
  argv: list[str],
  *,
  program: list[str],
  description: str = 'run a bro on the given input',
  input_transform: Optional[Callable[[str], str]] = None,
  export_task_id: bool = False,
  force_summon: bool = False,
) -> Optional[int]:
  """run the canonical one-shot launcher under `program`.

  aliases share this parser and execution path. `input_transform` supplies do-task's
  `/fix` wrapping, and `force_summon` supplies bare summon's implicit mode.
  """
  from cw import EFFORT_LEVELS

  parser = base.args.Parser(prog=' '.join(program), description=description)
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
    parser.add_argument('--slow', action='store_true', help=SLOW_HELP)
    parser.add_argument('--effort', choices=EFFORT_LEVELS, default=None, help=EFFORT_HELP)
    parser.add_argument('--summon', action='store_true', help=SUMMON_HELP)
    parser.add_argument('--in-place', action='store_true', help=IN_PLACE_HELP)
    parser.add_argument('--no-trails', dest='no_trails', action='store_true', help=NO_TRAILS_HELP)
    parser.add_exclusive_groups(['in_place'], ['no_trails'])
    parser.add_exclusive_groups(
      ['summon'],
      [
        'rich',
        'slow',
        'effort',
        'in_place',
        'no_trails',
        'grant_cred',
        'revoke_cred',
        'grant_summon',
        'revoke_summon',
      ],
    )
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
  parser.add_argument('--detach', action='store_true', help=DETACH_HELP)

  args = parser.parse(argv)
  if force_summon:
    args.update(
      summon=True,
      rich=False,
      slow=False,
      effort=None,
      in_place=False,
      no_trails=False,
      grant_cred=None,
      revoke_cred=None,
      grant_summon=None,
      revoke_summon=None,
    )
  shell_command = parser.reconstruct(args, prog=program)
  os.environ.setdefault('PPP_SHELL_COMMAND', ' '.join(shell_command))

  original_input = args['input']
  if export_task_id:
    from notion import parse_page_ref

    try:
      os.environ['CW_TASK_ID'] = parse_page_ref(original_input)
    except ValueError:
      pass
  input_text = original_input if input_transform is None else input_transform(original_input)

  if args['summon']:
    return _run_summoned(
      args['bro'], input_text, timeout=args['timeout'], into=args['into'], detach=args['detach']
    )
  if args['timeout'] is not None or args['detach']:
    print('--timeout/--detach require --summon', file=sys.stderr)
    return 1
  if os.environ.get('CW_IN_CONTAINER') is not None and not args['in_place']:
    print(
      'bro run refuses an implicit in-container run; pass --summon for an isolated run '
      "or --in-place to use this container's scope",
      file=sys.stderr,
    )
    return 1

  inner_args = [input_text]
  if args['rich']:
    inner_args.append('--rich')
  if args['slow']:
    inner_args.append('--slow')
  if args['effort'] is not None:
    inner_args.extend(['--effort', args['effort']])
  hopped = maybe_containerize(
    cli_name='bro-run' if program == ['bro', 'run'] else program[0],
    bro_name=args['bro'],
    inner_args=inner_args,
    inner_cli_name='ask',
    in_place=args['in_place'],
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
  except NotImplementedError as error:
    print(str(error), file=sys.stderr)
    return 1
  observer: Optional[Observer] = None
  if args['rich']:
    from llm.observer import RichConsoleRenderer

    observer = RichConsoleRenderer(prefix=bro.name)
  try:
    result = asyncio.run(bro.run(input_text, observer=observer))
  except BroRaised as error:
    print(f'raised: {error.reason}', file=sys.stderr)
    return 1
  print(result)
