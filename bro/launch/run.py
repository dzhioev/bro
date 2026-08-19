"""In-process one-shot bro launcher."""

import asyncio
import os
import sys
from typing import TYPE_CHECKING, Optional

import bro.base.args as base_args
from bro.base import log
from bro.bro import BaseBro, BroRaised
from bro.launch.llm_flags import (
  EFFORT_HELP,
  FAST_HELP,
  add_llm_flags,
  canonicalize,
  resolve_native,
  selection_from_args,
)
from bro.llm.llm import NativeLLMSpec
from bro.llm.mcp import HOLDS
from bro.llm.providers import LLMSelectionError
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


def run_llm_spec(bro_name: str, selection: 'LLMSelection') -> Optional[NativeLLMSpec]:
  """Resolve an explicit selection over the bro's declared native recipe."""
  if selection.is_empty():
    return None
  from bro.registry import get_class

  bro_class = get_class(bro_name)
  spec = resolve_native(bro_class.llm_spec, selection)
  return None if spec == bro_class.llm_spec else spec


def create_bro_for_run(bro_name: str, selection: 'LLMSelection') -> BaseBro:
  """Instantiate a bro with the run's optional recipe override."""
  from bro.registry import create_bro, get_class

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
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP.format('unattended'))

  args = parser.parse(argv)
  try:
    selection = selection_from_args(args)
    canonicalize(args, selection)
    bro = create_bro_for_run(args['bro'], selection)
  except (KeyError, NotImplementedError, LLMSelectionError) as error:
    log.error('%s', error)
    return 1
  os.environ.setdefault('BRO_SHELL_COMMAND', ' '.join(parser.reconstruct(args, prog=program)))

  from bro.launch.broxy import session_broxy

  with session_broxy():
    log.verbose('created bro %s', bro.name)
    observer = _ask_observer(bro.name)
    hold = args['hold'] if args['hold'] is not None else 'unattended'
    try:
      asyncio.run(bro.run(args['input'], observer=observer, surface='ask', hold=hold))
    except BroRaised:
      return 1
    except KeyboardInterrupt:
      log.error('interrupted')
      return 130
