"""In-process one-shot bro launcher."""

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import bro.base.args as base_args
from bro.base import log
from bro.bro import RAISE_EXIT_STATUS, BaseBro, BroRaised
from bro.launch.llm_flags import (
  EFFORT_HELP,
  FAST_HELP,
  add_llm_flags,
  selection_from_args,
)
from bro.llm.llm import LLMSpec
from bro.llm.providers import LLMSelectionError
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


def run_llm_spec(bro_name: str, selection: 'LLMSelection') -> Optional[LLMSpec]:
  """Resolve an explicit selection over the bro's declared native recipe."""
  from bro.registry import get_class

  bro_class = get_class(bro_name)
  if bro_class.llm_spec is None and selection.provider is None:
    raise LLMSelectionError(f'{bro_name} declares no LLM recipe: pass --llm provider:model')
  if selection.is_empty():
    return None
  spec = resolve_native(bro_class.llm_spec, selection)
  return None if spec == bro_class.llm_spec else spec


def decode_recipe(encoded: str) -> LLMSpec:
  """Read a recipe a launcher resolved and serialized, refusing one the native engine cannot run."""
  from bro.llm import wires

  try:
    spec = LLMSpec.from_dict(json.loads(encoded))
  except (KeyError, TypeError, ValueError) as error:
    raise LLMSelectionError(f'invalid recorded LLM recipe: {error}') from error
  if spec.WIRE not in wires.client_names():
    raise LLMSelectionError(f'recorded recipe {spec.TYPE} is not runnable by the bro harness')
  return spec


def create_bro_for_run(
  bro_name: str, selection: 'LLMSelection', recipe: Optional[LLMSpec] = None
) -> BaseBro:
  """Instantiate a bro on the launcher's resolved recipe, else the selection over its declaration."""
  from bro.registry import create_bro, get_class

  if recipe is not None:
    if not selection.is_empty():
      raise LLMSelectionError('--recipe cannot be combined with the LLM selection flags')
    return get_class(bro_name).create(recipe)
  spec = run_llm_spec(bro_name, selection)
  return create_bro(bro_name) if spec is None else get_class(bro_name).create(spec)


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
  parser.add_argument('--recipe', default=None, help=base_args.SUPPRESS)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP.format('unattended'))
  parser.add_argument(
    '--activity-file', type=Path, default=None, env=False, help=base_args.SUPPRESS
  )

  args = parser.parse(argv)
  try:
    selection = selection_from_args(args)
    recipe = None if args['recipe'] is None else decode_recipe(args['recipe'])
    bro = create_bro_for_run(args['bro'], selection, recipe)
  except (KeyError, NotImplementedError, LLMSelectionError) as error:
    log.error('%s', error)
    return 1

  from bro.launch.broxy import session_broxy

  with session_broxy():
    log.verbose('created bro %s', bro.name)
    observer = _ask_observer(bro.name)
    hold = args['hold'] if args['hold'] is not None else 'unattended'
    try:
      runner = Runner(bro, activity_file=args['activity_file'])
      asyncio.run(runner.run(args['input'], observer=observer, surface='ask', hold=hold))
    except BroRaised:
      return RAISE_EXIT_STATUS
    except KeyboardInterrupt:
      log.error('interrupted')
      return 130
