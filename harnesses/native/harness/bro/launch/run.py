"""In-process one-shot bro launcher."""

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import bro.base.args as base_args
from bro.base import log
from bro.bro import RAISE_EXIT_STATUS, BroRaised
from bro.launch.llm_flags import (
  EFFORT_HELP,
  FAST_HELP,
  add_llm_flags,
  selection_from_args,
)
from bro.llm.llm import Recipe
from bro.llm.providers import SELECTION_OPTIONS, LLMSelectionError, require_admitted
from bro.mcp import HOLDS
from bro.native.harness import resolve_native
from bro.native.runner import Runner
from bro.trails.display.config import OutputRoute, PresetName, preset
from bro.trails.display.core import DisplaySession
from bro.trails.display.live import LiveDisplayObserver
from bro.trails.display.terminal import StreamRenderer

if TYPE_CHECKING:
  from bro.llm.providers import LLMSelection

HOLD_HELP = (
  "the run's hold — the user-involvement level whose fragment lands in the system prompt "
  '(unattended = no human channel, detached = launched and left, attended = human watching, '
  'guided = human drives each step); default: {}'
)


def run_llm_spec(bro_name: str, selection: 'LLMSelection') -> Recipe:
  from ride.authority_reading import Reading
  from ride.authority_scope import resolve_recipe

  reading = Reading.take()
  configured = any('bro' in policy.config.llm for policy in reading.layers(bro_name))
  base = (
    resolve_recipe(reading, bro_name, 'bro').recipe
    if configured and selection.provider is None
    else None
  )
  if selection.is_empty() or (base is None and selection.provider is None):
    return resolve_recipe(reading, bro_name, 'bro').recipe
  spec = resolve_native(base, selection)
  require_admitted(spec)
  return spec


def add_resolved_llm_flag(parser: base_args.Parser) -> None:
  """Register the suppressed exact-recipe flag, exclusive with the selection flags."""
  parser.add_argument('--resolved-llm', default=None, env=False, help=base_args.SUPPRESS)
  parser.add_exclusive_groups(['resolved_llm'], ['llm', *SELECTION_OPTIONS])


def launch_llm_spec(bro_name: str, args: dict) -> Recipe:
  """The managed recipe verbatim, otherwise the live installation's stack."""
  from bro.llm import wires

  encoded = args['resolved_llm']
  if encoded is None:
    return run_llm_spec(bro_name, selection_from_args(args))
  try:
    spec = Recipe.from_dict(json.loads(encoded))
  except (KeyError, TypeError, ValueError) as error:
    raise LLMSelectionError(f'invalid --resolved-llm recipe: {error}') from error
  if spec.WIRE not in wires.client_names():
    raise LLMSelectionError(f'resolved recipe {spec.TYPE} is not runnable by the bro harness')
  require_admitted(spec)
  return spec


def _ask_observer(bro_name: str) -> LiveDisplayObserver:
  configuration = preset(PresetName.ASK, context_label=bro_name)
  destinations = dict.fromkeys(OutputRoute, sys.stderr)
  destinations[OutputRoute.REPLY] = sys.stdout
  return LiveDisplayObserver(DisplaySession(configuration, StreamRenderer(destinations)))


def run_main(argv: list[str], *, program: list[str]) -> Optional[int]:
  parser = base_args.Parser(prog=' '.join(program), description='run a bro on the given input')
  parser.add_argument('bro', help='bro name')
  parser.add_argument('input', help='input to send to the bro')
  add_llm_flags(parser, effort_help=EFFORT_HELP, fast_help=FAST_HELP)
  add_resolved_llm_flag(parser)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP.format('unattended'))
  parser.add_argument(
    '--activity-file', type=Path, default=None, env=False, help=base_args.SUPPRESS
  )

  args = parser.parse(argv)
  from bro.registry import create_bro

  try:
    recipe = launch_llm_spec(args['bro'], args)
    bro = create_bro(args['bro'])
  except (KeyError, NotImplementedError, ValueError) as error:
    log.error('%s', error)
    return 1

  from bro.launch.broxy import session_broxy

  with session_broxy():
    log.verbose('created bro %s', bro.name)
    observer = _ask_observer(bro.name)
    hold = args['hold'] if args['hold'] is not None else 'unattended'
    try:
      runner = Runner(bro, recipe, activity_file=args['activity_file'])
      asyncio.run(runner.run(args['input'], observer=observer, surface='ask', hold=hold))
    except BroRaised:
      return RAISE_EXIT_STATUS
    except KeyboardInterrupt:
      log.error('interrupted')
      return 130
