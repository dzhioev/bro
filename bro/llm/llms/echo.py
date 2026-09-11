from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Optional

import bro.llm.llm as llm_llm
from bro.llm import pricing

DEFAULT_MODEL = 'echo'

# Echo answers whatever it is asked with, so it has no model roster to name.
MODELS: dict[str, str] = {}

# and no API to fail against
FAILURE_SIGNATURES: tuple[llm_llm.FailureSignature, ...] = ()

PRICE_TABLE: Mapping[str, Any] = MappingProxyType({})
PRICE_TABLE_SHA256 = pricing.content_sha256({})


def price(
  model: str,
  usage: Mapping[str, Any],
  service_tier: Optional[str],
  table: Optional[Mapping[str, Any]] = None,
) -> Optional[Decimal]:
  """Echo makes no billable provider call."""
  return None


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
