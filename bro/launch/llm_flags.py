"""the LLM-selection flags every launch surface registers, and the preset store
behind `--llm`.

`--provider` / `--model` / `--effort` / `--fast` name the pieces of a recipe;
`--llm` names the whole thing at once and excludes them. A surface registers the
set with `add_llm_flags`, collapses what it parsed into the one canonical value
with `canonicalize`, and has its harness resolve that over the standing recipe
(`bro.harness.Harness.resolve_recipe`).

Kept apart from the launcher modules so a surface that only registers the flags
(the managed-workspace flag layer) can do it without pulling the launcher stack; the provider
roster is likewise imported at call time, not at load.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.base import host_config
from bro.base.args import Parser

if TYPE_CHECKING:
  from bro.llm.providers import LLMSelection

# the flags `--llm` speaks for, and so cannot be combined with.
_PIECE_FLAGS = ('provider', 'model', 'effort', 'fast')
_CURRENT_PROJECT = object()

PROVIDER_HELP = 'the provider that answers; selects its default model unless --model names one'
MODEL_HELP = (
  'the model that answers, by short name or full id — resolved within --provider when given, '
  'otherwise within the standing recipe’s provider'
)
LLM_HELP = (
  'the whole recipe in one value: `provider:model:effort` with an optional `+fast` suffix and '
  'any field left empty (`:fable5`, `::high`, `openai:sol:max+fast`), or a preset name from the '
  "project's [tool.bro.llm] table or the host's ~/.bro.json. excludes the four flags above"
)
FAST_HELP = (
  "run with the provider-specific fast-mode knob of the run's LLM recipe; "
  'a provider with no fast mode falls back to the plain recipe'
)
EFFORT_HELP = (
  "override the reasoning-effort knob of the run's LLM recipe with this neutral level, "
  "mapped onto the provider's own scale; without the flag the standing spec remains. "
  'errors when the provider has no effort knob'
)


def add_llm_flags(parser: Parser, *, effort_help: str, fast_help: str) -> None:
  """register the LLM-selection flag set — `--provider`, `--model`, `--effort`,
  `--fast`, and the `--llm` that excludes all four."""
  from bro.llm.llm import EFFORT_LEVELS

  parser.add_argument('--provider', default=None, metavar='NAME', help=PROVIDER_HELP)
  parser.add_argument('--model', default=None, metavar='NAME', help=MODEL_HELP)
  parser.add_argument('--effort', default=None, choices=EFFORT_LEVELS, help=effort_help)
  parser.add_argument('--fast', action='store_true', help=fast_help)
  parser.add_argument('--llm', default=None, metavar='RECIPE', help=LLM_HELP)
  parser.add_exclusive_groups(['llm'], list(_PIECE_FLAGS))


def presets(project: object = _CURRENT_PROJECT) -> dict[str, str]:
  """the project and host `--llm` presets for an explicit attachment.

  An omitted attachment preserves the cwd-aware behavior of non-ride launchers;
  explicit `None` is detached and reads host presets only.
  """
  from bro.workspace.project import project_sections, project_sections_at

  if project is _CURRENT_PROJECT:
    sections = project_sections()
  else:
    from bro.workspace.project import ProjectConfig

    if isinstance(project, ProjectConfig):
      sections = project.sections
    else:
      if project is not None and not isinstance(project, Path):
        raise TypeError(
          f'project must be a Path, ProjectConfig, or None, not {type(project).__name__}'
        )
      sections = project_sections_at(project)
  merged = dict(sections.get('llm', {}))
  for name, value in merged.items():
    if not isinstance(value, str) or value == '':
      raise ValueError(f'[tool.bro.llm] preset {name!r} must be a non-empty string')
  merged.update(host_config.llm_presets())
  return merged


def selection_from_args(args: dict, *, project: object = _CURRENT_PROJECT) -> 'LLMSelection':
  """Read the flags, expanding an uninstalled provider word through the preset table."""
  from bro.llm.providers import LLMSelection, is_preset, parse_with_presets

  value = args.get('llm')
  if value is None:
    return LLMSelection(
      provider=args.get('provider'),
      model=args.get('model'),
      effort=args.get('effort'),
      fast=args.get('fast', False),
    )
  return parse_with_presets(value, presets(project) if is_preset(value) else {})


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
