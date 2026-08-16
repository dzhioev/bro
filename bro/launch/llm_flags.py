"""the LLM-selection flags every launch surface registers, and the preset store
behind `--llm`.

`--provider` / `--model` / `--effort` / `--fast` name the pieces of a recipe;
`--llm` names the whole thing at once and excludes them. A surface registers the
set with `add_llm_flags`, collapses what it parsed into the one canonical value
with `canonicalize`, and resolves that over its own standing recipe
(`bro.llm.providers.resolve`).

Kept apart from the launcher modules so `bro.cw.flags` — which sits on every
`import bro.cw` — can register the flags without pulling the launcher stack; the
provider roster is likewise imported at call time, not at load.
"""

from typing import TYPE_CHECKING, Optional

from bro.base import host_config
from bro.base.args import Parser

if TYPE_CHECKING:
  from bro.llm.llm import NativeLLMSpec
  from bro.llm.llms.claude_code import LLMSpec as ClaudeCodeSpec
  from bro.llm.providers import LLMSelection

# the flags `--llm` speaks for, and so cannot be combined with.
_PIECE_FLAGS = ('provider', 'model', 'effort', 'fast')

PROVIDER_HELP = 'the provider that answers; selects its default model unless --model names one'
MODEL_HELP = (
  'the model that answers, by short name or full id — resolved within --provider when given, '
  'otherwise by the provider that serves the name'
)
LLM_HELP = (
  'the whole recipe in one value: `provider:model:effort` with an optional `+fast` suffix and '
  'any field left empty (`:fable5`, `::high`, `openai:sol:max+fast`), or a preset name from the '
  "project's [tool.bro.llm] table or the host's ~/.bro.json. excludes the four flags above"
)


def add_llm_flags(parser: Parser, *, effort_help: str, fast_help: str) -> None:
  """register the LLM-selection flag set — `--provider`, `--model`, `--effort`,
  `--fast`, and the `--llm` that excludes all four."""
  # imported here, not at module level: the provider roster pulls the llm
  # package, and this module sits on the cw flag path
  from bro.llm.llm import EFFORT_LEVELS
  from bro.llm.providers import known_names

  parser.add_argument('--provider', choices=known_names(), default=None, help=PROVIDER_HELP)
  parser.add_argument('--model', default=None, metavar='NAME', help=MODEL_HELP)
  parser.add_argument('--effort', default=None, choices=EFFORT_LEVELS, help=effort_help)
  parser.add_argument('--fast', action='store_true', help=fast_help)
  parser.add_argument('--llm', default=None, metavar='RECIPE', help=LLM_HELP)
  parser.add_exclusive_groups(['llm'], list(_PIECE_FLAGS))


def presets() -> dict[str, str]:
  """the `--llm` preset names in scope: the operated project's `[tool.bro.llm]`
  table, with the host's own `llm` table overriding it per name. Outside any
  project only the host's names are in scope."""
  from bro.workspace.project import project_sections

  merged = dict(project_sections().get('llm', {}))
  for name, value in merged.items():
    if not isinstance(value, str) or value == '':
      raise ValueError(f'[tool.bro.llm] preset {name!r} must be a non-empty string')
  merged.update(host_config.llm_presets())
  return merged


def selection_from_args(args: dict) -> 'LLMSelection':
  """the LLM selection `args` spells, with a preset name expanded to its value.

  A value that already spells a recipe is read as one and the preset table is
  never consulted — half of that table lives in the operated project, so a run
  handed the canonical value (`canonicalize`) resolves it with no repository
  around it. The names that lose to the grammar are exactly the provider names,
  the only bare words it accepts.
  """
  from bro.llm.providers import LLMSelection, LLMSelectionError, parse

  value = args.get('llm')
  if value is None:
    return LLMSelection(
      provider=args.get('provider'),
      model=args.get('model'),
      effort=args.get('effort'),
      fast=args.get('fast', False),
    )
  try:
    return parse(value)
  except LLMSelectionError:
    expanded = presets().get(value)
    if expanded is None:
      raise
  try:
    return parse(expanded)
  except LLMSelectionError as error:
    raise LLMSelectionError(f'--llm preset {value!r} ({expanded}): {error}') from error


def resolve_native(base: 'NativeLLMSpec', selection: 'LLMSelection') -> 'NativeLLMSpec':
  """the recipe a bro-native launcher runs: `selection` over the bro's own spec.

  A selection naming a harness that drives its own loop is refused rather than
  taken as a request to launch on that harness — these flags choose a model.
  """
  from bro.llm.llm import NativeLLMSpec
  from bro.llm.providers import LLMSelectionError, resolve

  spec = resolve(base, selection)
  if not isinstance(spec, NativeLLMSpec):
    raise LLMSelectionError(
      f'{spec.TYPE} runs its own agent loop, so a bro cannot be launched against it here; '
      f'run the bro under that harness with `cw ss --raw --llm {selection.format()}`'
    )
  return spec


def resolve_claude(selection: 'LLMSelection') -> 'ClaudeCodeSpec':
  """the recipe a claude session runs: `selection` over Claude Code's own default.

  A selection naming another provider is refused — the session is Claude Code.
  """
  from bro.llm.llms.claude_code import LLMSpec as ClaudeCodeSpec
  from bro.llm.providers import LLMSelectionError, resolve

  spec = resolve(ClaudeCodeSpec(), selection)
  if not isinstance(spec, ClaudeCodeSpec):
    raise LLMSelectionError(
      f'a claude session runs Claude Code, not {spec.TYPE}; run a bro against it with '
      f'`bro run <bro> --llm {selection.format()}`'
    )
  return spec


def canonicalize(args: dict, selection: 'LLMSelection') -> Optional[str]:
  """write `selection` back into `args` as the single canonical `--llm` value and
  return it (None when it names nothing).

  Mutates `args`: the four piece flags go back to their parser defaults and `llm`
  holds the whole selection. So a surface reconstructing its argv forwards one
  flag, and nothing downstream reads a preset table or infers a provider a second
  time.
  """
  for key in _PIECE_FLAGS:
    if key in args:
      args[key] = False if key == 'fast' else None
  args['llm'] = None if selection.is_empty() else selection.format()
  return args['llm']


def drop_piece_flags(args: dict) -> None:
  """remove the four piece flags from `args` entirely — for a surface that splats
  it into a record carrying only the canonical `llm` value."""
  for key in _PIECE_FLAGS:
    args.pop(key, None)
