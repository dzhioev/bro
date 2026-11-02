"""In-process one-shot bro launcher."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import bro.base.args as base_args
from bro.base import log
from bro.bro import RAISE_EXIT_STATUS, BroRaised
from bro.launch.llm_flags import add_llm_flags
from bro.llm.providers import RecipeError, require_admitted
from bro.llm.resolved import ResolvedRecipe
from bro.mcp import HOLDS
from bro.native.runner import Runner
from bro.trails.display.config import OutputRoute, PresetName, preset
from bro.trails.display.core import DisplaySession
from bro.trails.display.live import LiveDisplayObserver
from bro.trails.display.terminal import StreamRenderer

HOLD_HELP = (
  "the run's hold — the user-involvement level whose fragment lands in the system prompt "
  '(unattended = no human channel, detached = launched and left, attended = human watching, '
  'guided = human drives each step); default: {}'
)


def add_resolved_recipe_flag(parser: base_args.Parser) -> None:
  """Register the suppressed exact-recipe flag, exclusive with --llm."""
  parser.add_argument('--resolved-recipe', default=None, env=False, help=base_args.SUPPRESS)
  parser.add_exclusive_groups(['resolved_recipe'], ['llm'])


def launch_recipe(bro_name: str, args: dict) -> ResolvedRecipe:
  """The managed recipe verbatim, otherwise the live installation's stack."""
  from bro.llm import wires

  encoded = args['resolved_recipe']
  if encoded is None:
    from ride.authority_reading import Reading
    from ride.authority_scope import resolve_recipe

    explicit = None if args['llm'] is None else (args['llm'], '--llm')
    return resolve_recipe(Reading.take(), bro_name, 'bro', explicit)
  try:
    resolved = ResolvedRecipe.from_dict(json.loads(encoded))
    spec = resolved.recipe
  except (KeyError, TypeError, ValueError) as error:
    raise RecipeError(f'invalid --resolved-recipe recipe: {error}') from error
  if spec.WIRE not in wires.client_names():
    raise RecipeError(f'resolved recipe {spec.TYPE} is not runnable by the bro harness')
  require_admitted(spec)
  return resolved


def _ask_observer(bro_name: str) -> LiveDisplayObserver:
  configuration = preset(PresetName.ASK, context_label=bro_name)
  destinations = dict.fromkeys(OutputRoute, sys.stderr)
  destinations[OutputRoute.REPLY] = sys.stdout
  return LiveDisplayObserver(DisplaySession(configuration, StreamRenderer(destinations)))


def run_main(argv: list[str], *, program: list[str]) -> Optional[int]:
  parser = base_args.Parser(prog=' '.join(program), description='run a bro on the given input')
  parser.add_argument('bro', help='bro name')
  parser.add_argument('input', help='input to send to the bro')
  add_llm_flags(parser)
  add_resolved_recipe_flag(parser)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP.format('unattended'))
  parser.add_argument(
    '--activity-file', type=Path, default=None, env=False, help=base_args.SUPPRESS
  )

  args = parser.parse(argv)
  from bro.registry import create_bro

  try:
    recipe = launch_recipe(args['bro'], args)
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
