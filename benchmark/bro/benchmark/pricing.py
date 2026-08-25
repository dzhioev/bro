"""Benchmark-owned model prices and exact per-call cost calculation."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Optional

from bro.llm.usage import CLASSES, Counts

_ONE_MILLION = Decimal(1_000_000)


class UnpricedModelError(ValueError):
  pass


@dataclass(frozen=True)
class TokenPrices:
  """USD per million tokens for the four normalized billed classes."""

  input: Decimal
  cache_write: Decimal
  cache_read: Decimal
  output: Decimal

  def for_class(self, token_class: str) -> Decimal:
    if token_class not in CLASSES:
      raise ValueError(f'unknown billed token class {token_class!r}')
    return getattr(self, token_class)


@dataclass(frozen=True)
class ModelPrice:
  short_context: TokenPrices
  long_context: TokenPrices
  long_context_threshold: int
  service_tier: str
  source: str
  as_of: date

  def prices_for(self, counts: Mapping[str, int]) -> TokenPrices:
    input_tokens = sum(counts.get(token_class, 0) for token_class in CLASSES[:-1])
    if input_tokens > self.long_context_threshold:
      return self.long_context
    return self.short_context


_OPENAI_PRICING = 'https://developers.openai.com/api/docs/pricing'

PRICE_TABLE: Mapping[str, ModelPrice] = MappingProxyType(
  {
    'gpt-5.6-terra': ModelPrice(
      short_context=TokenPrices(
        input=Decimal('2.00'),
        cache_write=Decimal('2.50'),
        cache_read=Decimal('0.20'),
        output=Decimal('12.00'),
      ),
      long_context=TokenPrices(
        input=Decimal('4.00'),
        cache_write=Decimal('5.00'),
        cache_read=Decimal('0.40'),
        output=Decimal('18.00'),
      ),
      long_context_threshold=272_000,
      service_tier='standard',
      source=_OPENAI_PRICING,
      as_of=date(2026, 8, 25),
    )
  }
)


def _validated_counts(counts: Mapping[str, int]) -> Counts:
  unknown_classes = set(counts) - set(CLASSES)
  if len(unknown_classes) > 0:
    raise ValueError(f'usage contains unknown billed token classes: {sorted(unknown_classes)}')
  validated: Counts = {}
  for token_class in CLASSES:
    count = counts.get(token_class, 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
      raise ValueError(f'{token_class} token count must be a non-negative int')
    validated[token_class] = count
  return validated


def call_cost_usd(model: str, counts: Mapping[str, int]) -> Decimal:
  """Price one standard-service call; aggregated counts must stay split by call."""
  validated = _validated_counts(counts)
  price = PRICE_TABLE.get(model)
  if price is None:
    raise UnpricedModelError(f'no benchmark price for model {model!r}')
  rates = price.prices_for(validated)
  return (
    sum(
      (Decimal(validated[token_class]) * rates.for_class(token_class) for token_class in CLASSES),
      Decimal(0),
    )
    / _ONE_MILLION
  )


def optional_call_cost_usd(model: str, counts: Mapping[str, int]) -> Optional[Decimal]:
  try:
    return call_cost_usd(model, counts)
  except UnpricedModelError:
    return None
