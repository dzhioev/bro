"""DeepSeek recipes and peak list pricing."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from bro.llm import pricing
from bro.llm.llm import FailureSignature, Recipe as BaseRecipe

FAILURE_SIGNATURES = (
  FailureSignature(r'Error code: 402', 'usage-limit'),
  FailureSignature(r'openai\.RateLimitError', 'rate-limit'),
  FailureSignature(r'openai\.InternalServerError', 'server-error'),
  FailureSignature(r'openai\.(APIConnectionError|APITimeoutError)', 'network'),
  FailureSignature(r'openai\.(AuthenticationError|PermissionDeniedError)', 'authentication'),
  FailureSignature(r'openai\.NotFoundError', 'model-not-found'),
  FailureSignature(
    r'openai\.(BadRequestError|UnprocessableEntityError|APIStatusError)', 'unknown-api'
  ),
)


@dataclass(frozen=True)
class TokenRates:
  cache_hit: Decimal
  cache_miss: Decimal
  output: Decimal

  def content(self) -> dict[str, str]:
    return pricing.decimal_strings(
      {'cache_hit': self.cache_hit, 'cache_miss': self.cache_miss, 'output': self.output}
    )


@dataclass(frozen=True)
class PriceTable:
  source: str
  as_of: date
  models: Mapping[str, TokenRates]

  @property
  def sha256(self) -> str:
    return pricing.content_sha256(
      {
        'source': self.source,
        'as_of': self.as_of.isoformat(),
        'models': {model: rates.content() for model, rates in self.models.items()},
      }
    )


def price_table_from_content(source: str, as_of: str, models: Mapping[str, Any]) -> PriceTable:
  if not source or source.strip() != source:
    raise ValueError('price table source must be a non-empty trimmed string')
  parsed = {}
  for model, content in models.items():
    if not isinstance(model, str) or not model:
      raise ValueError('price table model names must be non-empty strings')
    if not isinstance(content, Mapping) or set(content) != {'cache_hit', 'cache_miss', 'output'}:
      raise ValueError('model rates must contain exactly cache_hit, cache_miss and output')
    rates = {}
    for name, raw in content.items():
      if not isinstance(raw, str):
        raise ValueError(f'{name} must be a decimal string')
      try:
        rate = Decimal(raw)
      except ArithmeticError as error:
        raise ValueError(f'{name} must be a non-negative finite decimal') from error
      if not rate.is_finite() or rate < 0:
        raise ValueError(f'{name} must be a non-negative finite decimal')
      rates[name] = rate
    parsed[model] = TokenRates(**rates)
  return PriceTable(source, date.fromisoformat(as_of), MappingProxyType(parsed))


# Off-peak rates are half of these peak rates; call timestamps do not determine billing here.
PRICE_TABLE = price_table_from_content(
  'https://api-docs.deepseek.com/quick_start/pricing/',
  '2026-10-29',
  {
    'deepseek-v4-pro': {'cache_hit': '0.044', 'cache_miss': '1.32', 'output': '3.96'},
    'deepseek-flash': {'cache_hit': '0.006', 'cache_miss': '0.3', 'output': '1.2'},
  },
)
PRICE_TABLE_SHA256 = PRICE_TABLE.sha256


def _count(usage: Mapping[str, Any], name: str) -> int:
  value = usage.get(name)
  if type(value) is not int or value < 0:
    raise ValueError(f'{name} must be a non-negative int')
  return value


def price(
  model: str,
  usage: Mapping[str, Any],
  service_tier: str | None,
  table: PriceTable | None = None,
) -> Decimal | None:
  """Return a peak-rate upper bound in USD, or None for an unpriced model."""
  selected = PRICE_TABLE if table is None else table
  rates = selected.models.get(model)
  if rates is None:
    return None
  pricing.warn_if_stale('deepseek', selected.as_of)
  prompt = _count(usage, 'prompt_tokens')
  hit = _count(usage, 'prompt_cache_hit_tokens')
  miss = _count(usage, 'prompt_cache_miss_tokens')
  output = _count(usage, 'completion_tokens')
  if hit + miss != prompt:
    raise ValueError('cache-hit and cache-miss tokens must sum to prompt_tokens')
  return (hit * rates.cache_hit + miss * rates.cache_miss + output * rates.output) / 1_000_000


@dataclass(frozen=True)
class Recipe(BaseRecipe):
  TYPE: ClassVar[str] = 'deepseek'
  WIRE: ClassVar[str] = 'chat'
  # DeepSeek models/pricing page and vision guide, read 2026-10-31.
  MODEL_INPUTS: ClassVar[Mapping[str, frozenset[str]]] = {
    'deepseek-v4-pro': frozenset(),
    'deepseek-flash': frozenset({'image/jpeg', 'image/png', 'image/gif', 'image/webp'}),
  }
  RETIRED_MODELS: ClassVar[Mapping[str, date]] = {}
  ENDPOINT: ClassVar[str] = 'https://api.deepseek.com'

  reasoning_effort: Literal['none', 'low', 'high', 'max'] | None = None

  def request_options(self) -> dict:
    if self.reasoning_effort is None:
      return {}
    if self.reasoning_effort == 'none':
      return {'thinking': {'type': 'disabled'}}
    return {'thinking': {'type': 'enabled'}, 'reasoning_effort': self.reasoning_effort}

  def is_transient(self, error: Exception) -> bool:
    from openai import APIConnectionError, APIStatusError

    return isinstance(error, APIConnectionError) or (
      isinstance(error, APIStatusError) and (error.status_code == 429 or error.status_code >= 500)
    )

  @classmethod
  def needed_secrets(cls) -> tuple[str, ...]:
    return (cls.TYPE,)
