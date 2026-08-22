from dataclasses import dataclass
from typing import ClassVar

import bro.llm.llm as llm_llm

DEFAULT_MODEL = 'echo'

# Echo answers whatever it is asked with, so it has no model roster to name.
MODELS: dict[str, str] = {}

# and no API to fail against
FAILURE_SIGNATURES: tuple[llm_llm.FailureSignature, ...] = ()


@dataclass(frozen=True)
class LLMSpec(llm_llm.NativeLLMSpec):
  """trivial spec for Echo. inherits the raising base `.fast` since echo has
  no fast-mode equivalent."""

  TYPE: ClassVar[str] = 'echo'

  model: str = DEFAULT_MODEL

  def dump(self) -> dict:
    return {'type': self.TYPE, 'model': self.model}

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    return cls(model=data['model'])
