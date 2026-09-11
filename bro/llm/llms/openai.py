# Every bro module constructs an `LLMSpec` at class-definition time, so this
# declaration module must not import the provider SDK or native client.

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Optional, Self, cast, get_args

import bro.llm.llm as llm_llm
from bro.llm import pricing

ServiceTier = Literal['auto', 'default', 'flex', 'priority']
_VALID_SERVICE_TIERS: frozenset[str] = frozenset(get_args(ServiceTier))
# local mirror of the Literal inside openai's `ReasoningEffort` (which the SDK
# wraps in Optional); spelled out so spec validation needs no openai import. a
# sync test asserts the values against the SDK's type.
ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
_VALID_REASONING_EFFORTS: frozenset[str] = frozenset(get_args(ReasoningEffort))

DEFAULT_MODEL = 'gpt-5.6-terra'

# `--model` short names for this provider's models.
MODELS: dict[str, str] = {
  'terra': 'gpt-5.6-terra',
  'sol': 'gpt-5.6-sol',
}

# how this provider's client failures read in a run's error output
FAILURE_SIGNATURES: tuple[llm_llm.FailureSignature, ...] = (
  llm_llm.FailureSignature(r'openai\.RateLimitError', 'rate-limit'),
  llm_llm.FailureSignature(r'openai\.InternalServerError', 'server-error'),
  llm_llm.FailureSignature(r'openai\.(APIConnectionError|APITimeoutError)', 'network'),
  llm_llm.FailureSignature(
    r'openai\.(AuthenticationError|PermissionDeniedError)', 'authentication'
  ),
  llm_llm.FailureSignature(r'openai\.NotFoundError', 'model-not-found'),
  llm_llm.FailureSignature(r'insufficient_quota', 'usage-limit'),
  llm_llm.FailureSignature(r'openai\.APIStatusError', 'unknown-api'),
)

_ONE_MILLION = Decimal(1_000_000)
_LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True)
class TokenRates:
  """USD per million Responses API tokens, in OpenAI's billing classes."""

  input: Decimal
  cached_input: Decimal
  cache_write: Decimal
  output: Decimal

  def content(self) -> dict[str, str]:
    return pricing.decimal_strings(
      {
        'input': self.input,
        'cached_input': self.cached_input,
        'cache_write': self.cache_write,
        'output': self.output,
      }
    )


@dataclass(frozen=True)
class ContextRates:
  short: TokenRates
  long: TokenRates

  def content(self) -> dict[str, dict[str, str]]:
    return {'short': self.short.content(), 'long': self.long.content()}


@dataclass(frozen=True)
class ModelRates:
  long_context_threshold: int
  service_tiers: Mapping[str, ContextRates]

  def content(self) -> dict[str, Any]:
    return {
      'long_context_threshold': self.long_context_threshold,
      'service_tiers': {
        service_tier: rates.content() for service_tier, rates in self.service_tiers.items()
      },
    }


@dataclass(frozen=True)
class PriceTable:
  source: str
  as_of: date
  models: Mapping[str, ModelRates]

  @property
  def sha256(self) -> str:
    return pricing.content_sha256(
      {
        'source': self.source,
        'as_of': self.as_of.isoformat(),
        'models': {model: rates.content() for model, rates in self.models.items()},
      }
    )


def _content_mapping(value: Any, field: str) -> Mapping[str, Any]:
  if not isinstance(value, Mapping):
    raise ValueError(f'{field} must be an object')
  return value


def _content_decimal(value: Any, field: str) -> Decimal:
  if not isinstance(value, str):
    raise ValueError(f'{field} must be a decimal string')
  try:
    decimal = Decimal(value)
  except ArithmeticError as error:
    raise ValueError(f'{field} must be a non-negative finite decimal string') from error
  if not decimal.is_finite() or decimal < 0:
    raise ValueError(f'{field} must be a non-negative finite decimal string')
  return decimal


def _content_token_rates(value: Any, field: str) -> TokenRates:
  content = _content_mapping(value, field)
  expected = {'input', 'cached_input', 'cache_write', 'output'}
  if set(content) != expected:
    raise ValueError(f'{field} must contain exactly {sorted(expected)}')
  return TokenRates(
    input=_content_decimal(content['input'], f'{field}.input'),
    cached_input=_content_decimal(content['cached_input'], f'{field}.cached_input'),
    cache_write=_content_decimal(content['cache_write'], f'{field}.cache_write'),
    output=_content_decimal(content['output'], f'{field}.output'),
  )


def price_table_from_content(source: str, as_of: str, models: Mapping[str, Any]) -> PriceTable:
  """Reconstruct an immutable table from this provider's serialized vocabulary."""
  if source == '' or source.strip() != source:
    raise ValueError('price table source must be a non-empty trimmed string')
  try:
    effective_date = date.fromisoformat(as_of)
  except ValueError as error:
    raise ValueError('price table as_of must be an ISO date') from error
  parsed_models: dict[str, ModelRates] = {}
  for model, raw_model in models.items():
    if not isinstance(model, str) or model == '':
      raise ValueError('price table model names must be non-empty strings')
    model_content = _content_mapping(raw_model, f'price table model {model}')
    expected = {'long_context_threshold', 'service_tiers'}
    if set(model_content) != expected:
      raise ValueError(f'price table model {model} must contain exactly {sorted(expected)}')
    threshold = model_content['long_context_threshold']
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
      raise ValueError(f'price table model {model} long_context_threshold must be non-negative')
    raw_tiers = _content_mapping(
      model_content['service_tiers'], f'price table model {model} service_tiers'
    )
    parsed_tiers: dict[str, ContextRates] = {}
    for tier, raw_context in raw_tiers.items():
      if not isinstance(tier, str) or tier == '':
        raise ValueError('price table service tier names must be non-empty strings')
      context = _content_mapping(raw_context, f'price table model {model} service tier {tier}')
      if set(context) != {'short', 'long'}:
        raise ValueError(
          f'price table model {model} service tier {tier} must contain short and long'
        )
      parsed_tiers[tier] = ContextRates(
        short=_content_token_rates(
          context['short'], f'price table model {model} service tier {tier}.short'
        ),
        long=_content_token_rates(
          context['long'], f'price table model {model} service tier {tier}.long'
        ),
      )
    parsed_models[model] = ModelRates(threshold, MappingProxyType(parsed_tiers))
  return PriceTable(source, effective_date, MappingProxyType(parsed_models))


# Rates are copied from the vendor page and updated by hand in a PR together
# with this date. Pricing never fetches vendor data at run time.
PRICE_TABLE = PriceTable(
  source='https://developers.openai.com/api/docs/pricing',
  as_of=date(2026, 9, 11),
  models=MappingProxyType(
    {
      'gpt-5.6-terra': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=TokenRates(
                input=Decimal('2.00'),
                cached_input=Decimal('0.20'),
                cache_write=Decimal('2.50'),
                output=Decimal('12.00'),
              ),
              long=TokenRates(
                input=Decimal('4.00'),
                cached_input=Decimal('0.40'),
                cache_write=Decimal('5.00'),
                output=Decimal('18.00'),
              ),
            ),
            'priority': ContextRates(
              short=TokenRates(
                input=Decimal('4.00'),
                cached_input=Decimal('0.40'),
                cache_write=Decimal('5.00'),
                output=Decimal('24.00'),
              ),
              long=TokenRates(
                input=Decimal('8.00'),
                cached_input=Decimal('0.80'),
                cache_write=Decimal('10.00'),
                output=Decimal('36.00'),
              ),
            ),
          }
        ),
      )
    }
  ),
)
PRICE_TABLE_SHA256 = PRICE_TABLE.sha256


def _count(value: Any, field: str) -> int:
  if not isinstance(value, int) or isinstance(value, bool) or value < 0:
    raise ValueError(f'{field} must be a non-negative int')
  return value


def _service_tier_name(service_tier: Optional[str]) -> Optional[str]:
  if service_tier in {None, 'auto', 'default'}:
    return 'standard'
  if service_tier in {'fast', 'priority'}:
    return 'priority'
  return None


def price(
  model: str,
  usage: Mapping[str, Any],
  service_tier: Optional[str],
  table: Optional[PriceTable] = None,
) -> Optional[Decimal]:
  """Price one Responses API call from its raw usage and effective tier."""
  selected = PRICE_TABLE if table is None else table
  model_rates = selected.models.get(model)
  tier = _service_tier_name(service_tier)
  if model_rates is None or tier is None:
    return None
  context_rates = model_rates.service_tiers.get(tier)
  if context_rates is None:
    return None
  pricing.warn_if_stale('openai', selected.as_of)

  input_tokens = _count(usage.get('input_tokens'), 'input_tokens')
  output_tokens = _count(usage.get('output_tokens'), 'output_tokens')
  raw_details = usage.get('input_tokens_details', {})
  if not isinstance(raw_details, Mapping):
    raise ValueError('input_tokens_details must be an object')
  cached_tokens = _count(raw_details.get('cached_tokens', 0), 'input_tokens_details.cached_tokens')
  cache_write_tokens = _count(
    raw_details.get('cache_write_tokens', 0), 'input_tokens_details.cache_write_tokens'
  )
  fresh_tokens = input_tokens - cached_tokens - cache_write_tokens
  if fresh_tokens < 0:
    raise ValueError('cached and cache-write tokens exceed input_tokens')

  rates = (
    context_rates.long if input_tokens > model_rates.long_context_threshold else context_rates.short
  )
  return (
    Decimal(fresh_tokens) * rates.input
    + Decimal(cached_tokens) * rates.cached_input
    + Decimal(cache_write_tokens) * rates.cache_write
    + Decimal(output_tokens) * rates.output
  ) / _ONE_MILLION


# neutral effort level (`LLMSpec.with_effort`) → Responses API reasoning_effort.
_EFFORT_TO_REASONING_EFFORT: dict[str, ReasoningEffort] = {
  'low': 'low',
  'medium': 'medium',
  'high': 'high',
  'xhigh': 'xhigh',
  'max': 'max',
}


@dataclass(frozen=True)
class LLMSpec(llm_llm.NativeLLMSpec):
  """spec for the OpenAI Responses API.

  service_tier='priority' is the analog of Claude Code's /fast — same model
  and quality, higher per-token price, faster and more consistent generation.
  Toggle it through `.fast()` rather than constructing a new spec by hand.

  compact_threshold (opt-in) bounds context growth in long runs: the native
  client passes it as server-side context-management policy. None (the default)
  leaves growth unbounded. GPT-5-family models take at most 272k input tokens (400k window
  minus the 128k output reservation), so a value like 200_000 leaves tool-loop
  turns room to grow between the threshold crossing and the compaction pass.
  Size it far above per-turn growth: with the threshold near the working
  context size the server recompacts repeatedly within one response (observed
  live: 10 passes per call, ~5x billed input, minutes of latency).
  """

  TYPE: ClassVar[str] = 'openai'

  model: str = DEFAULT_MODEL
  reasoning_effort: Optional[ReasoningEffort] = None
  service_tier: Optional[ServiceTier] = None
  compact_threshold: Optional[int] = None

  def __post_init__(self):
    if self.service_tier is not None and self.service_tier not in _VALID_SERVICE_TIERS:
      raise ValueError(
        f'invalid service_tier {self.service_tier!r}; expected one of '
        f'{sorted(_VALID_SERVICE_TIERS)} or None'
      )
    if self.reasoning_effort is not None and self.reasoning_effort not in _VALID_REASONING_EFFORTS:
      raise ValueError(
        f'invalid reasoning_effort {self.reasoning_effort!r}; expected one of '
        f'{sorted(_VALID_REASONING_EFFORTS)} or None'
      )
    if self.compact_threshold is not None and self.compact_threshold <= 0:
      raise ValueError(
        f'invalid compact_threshold {self.compact_threshold!r}; expected a positive int or None'
      )

  def fast(self) -> Self:
    return dataclasses.replace(self, service_tier='priority')

  def with_effort(self, effort: str) -> Self:
    reasoning_effort = _EFFORT_TO_REASONING_EFFORT.get(effort)
    if reasoning_effort is None:
      raise ValueError(
        f'unknown effort level {effort!r}; expected one of {list(_EFFORT_TO_REASONING_EFFORT)}'
      )
    return dataclasses.replace(self, reasoning_effort=reasoning_effort)

  def needed_secrets(self) -> tuple[str, ...]:
    return ('openai',)

  def dump(self) -> dict:
    return {
      'type': self.TYPE,
      'model': self.model,
      'reasoning_effort': self.reasoning_effort,
      'service_tier': self.service_tier,
      'compact_threshold': self.compact_threshold,
    }

  @classmethod
  def _from_dict_impl(cls, data: dict) -> LLMSpec:
    # __post_init__ revalidates these against the Literal types; the cast keeps
    # the static checker happy on the JSON-derived path where pyright sees
    # `str | None`.
    return cls(
      model=data['model'],
      reasoning_effort=cast(Optional[ReasoningEffort], data.get('reasoning_effort')),
      service_tier=cast(Optional[ServiceTier], data.get('service_tier')),
      compact_threshold=data.get('compact_threshold'),
    )
