from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Optional

import bro.llm.llm as llm_llm
from bro.llm import pricing

# and no API to fail against
FAILURE_SIGNATURES: tuple[llm_llm.FailureSignature, ...] = ()


@dataclass(frozen=True)
class PriceTable:
  models: Mapping[str, Decimal]


PRICE_TABLE = PriceTable(MappingProxyType({'echo': Decimal(0)}))
PRICE_TABLE_SHA256 = pricing.content_sha256(
  {model: str(rate) for model, rate in PRICE_TABLE.models.items()}
)


def price(
  model: str,
  usage: Mapping[str, Any],
  service_tier: Optional[str],
  table: Optional[PriceTable] = None,
) -> Optional[Decimal]:
  """Echo makes no billable provider call."""
  return (PRICE_TABLE if table is None else table).models.get(model)


@dataclass(frozen=True)
class Recipe(llm_llm.Recipe):
  """A diagnostic recipe for the echo wire."""

  TYPE: ClassVar[str] = 'echo'
  WIRE: ClassVar[str] = 'echo'
  MODEL_INPUTS: ClassVar[Mapping[str, frozenset[str]]] = {'echo': frozenset()}

  RETIRED_MODELS: ClassVar[Mapping[str, date]] = {}
