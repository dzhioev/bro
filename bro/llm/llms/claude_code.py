"""Claude Code as an LLM provider: the model and knobs a claude session runs
under, carried as an `LLMSpec` like every other provider's recipe.

Claude Code drives its own agent loop, so this is a recipe and nothing more — it
builds no client, and a session surface reads the fields off it.
"""

import dataclasses
from dataclasses import dataclass
from typing import ClassVar, Optional, Self

import bro.llm.llm as llm_llm

DEFAULT_MODEL = 'fable'

# `--model` short names for this provider's models. `fable` is not a model id
# but Claude Code's own family alias, which it resolves to whichever model it
# currently defaults that family to — so it stands for itself.
MODELS: dict[str, str] = {
  'opus5': 'claude-opus-5',
  'sonnet5': 'claude-sonnet-5',
  'fable': 'fable',
  'fable5': 'claude-fable-5',
  'fable51': 'claude-fable-5-1',
  'haiku45': 'claude-haiku-4-5-20251001',
}

# the harness drives its own loop and surfaces failures as session output, not
# as exception spellings a caller's error scan could classify
FAILURE_SIGNATURES: tuple[llm_llm.FailureSignature, ...] = ()

DEFAULT_EFFORT = 'xhigh'


@dataclass(frozen=True)
class LLMSpec(llm_llm.LLMSpec):
  """spec for a Claude Code session.

  `effort` is a neutral level (`llm.EFFORT_LEVELS`), which claude's own
  `--effort` takes unmapped. `fast_mode` is claude's /fast — the field is spelled
  apart from the `fast()` knob setter it backs, which a same-named field would
  shadow.
  """

  TYPE: ClassVar[str] = 'claude-code'

  model: str = DEFAULT_MODEL
  effort: Optional[str] = DEFAULT_EFFORT
  fast_mode: bool = False

  def __post_init__(self):
    if self.effort is not None and self.effort not in llm_llm.EFFORT_LEVELS:
      raise ValueError(
        f'invalid effort {self.effort!r}; expected one of {list(llm_llm.EFFORT_LEVELS)} or None'
      )

  def fast(self) -> Self:
    return dataclasses.replace(self, fast_mode=True)

  def with_effort(self, effort: str) -> Self:
    if effort not in llm_llm.EFFORT_LEVELS:
      raise ValueError(
        f'unknown effort level {effort!r}; expected one of {list(llm_llm.EFFORT_LEVELS)}'
      )
    return dataclasses.replace(self, effort=effort)

  def dump(self) -> dict:
    return {
      'type': self.TYPE,
      'model': self.model,
      'effort': self.effort,
      'fast_mode': self.fast_mode,
    }

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    return cls(
      model=data['model'],
      effort=data.get('effort'),
      fast_mode=data.get('fast_mode', False),
    )
