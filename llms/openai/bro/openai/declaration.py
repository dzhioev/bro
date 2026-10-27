from __future__ import annotations

import dataclasses
import re
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
  'sol': 'gpt-6.1-sol',
  'luna': 'gpt-6-luna',
  'astra': 'gpt-6-astra',
  'terra': 'gpt-5.6-terra',
  'luna5.6': 'gpt-5.6-luna',
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
# a dated snapshot id, which the vendor page prices under its undated name
# unless it lists the snapshot as a model of its own
_SNAPSHOT_DATE = re.compile(r'-\d{4}-\d{2}-\d{2}$')


@dataclass(frozen=True)
class TokenRates:
  """USD per million Responses API tokens, in OpenAI's billing classes.

  A class left None is one the vendor publishes no rate for: the model does not
  bill it, so its usage never reports tokens in it.
  """

  input: Decimal
  cached_input: Optional[Decimal]
  cache_write: Optional[Decimal]
  output: Decimal

  def content(self) -> dict[str, Optional[str]]:
    rates = {
      'input': self.input,
      'cached_input': self.cached_input,
      'cache_write': self.cache_write,
      'output': self.output,
    }
    return {name: None if rate is None else str(rate) for name, rate in rates.items()}


@dataclass(frozen=True)
class ContextRates:
  """a service tier's rates; `long` is what a call over its model's long-context
  threshold pays, None where the tier publishes no long-context rate."""

  short: TokenRates
  long: Optional[TokenRates] = None

  def content(self) -> dict[str, Optional[dict[str, Optional[str]]]]:
    return {
      'short': self.short.content(),
      'long': None if self.long is None else self.long.content(),
    }


@dataclass(frozen=True)
class ModelRates:
  """a model's rates by service tier; `long_context_threshold` is None for a
  model priced the same at every prompt length."""

  service_tiers: Mapping[str, ContextRates]
  long_context_threshold: Optional[int] = None

  def __post_init__(self) -> None:
    if self.long_context_threshold is None and any(
      rates.long is not None for rates in self.service_tiers.values()
    ):
      raise ValueError('long-context rates need a long_context_threshold')

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

  def rates(self, model: str) -> Optional[ModelRates]:
    """the model's rates, a dated snapshot falling back to its undated name."""
    rates = self.models.get(model)
    if rates is None and _SNAPSHOT_DATE.search(model):
      rates = self.models.get(_SNAPSHOT_DATE.sub('', model))
    return rates


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


def _content_optional_decimal(value: Any, field: str) -> Optional[Decimal]:
  return None if value is None else _content_decimal(value, field)


def _content_token_rates(value: Any, field: str) -> TokenRates:
  content = _content_mapping(value, field)
  expected = {'input', 'cached_input', 'cache_write', 'output'}
  if set(content) != expected:
    raise ValueError(f'{field} must contain exactly {sorted(expected)}')
  return TokenRates(
    input=_content_decimal(content['input'], f'{field}.input'),
    cached_input=_content_optional_decimal(content['cached_input'], f'{field}.cached_input'),
    cache_write=_content_optional_decimal(content['cache_write'], f'{field}.cache_write'),
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
    if threshold is not None and (
      not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0
    ):
      raise ValueError(
        f'price table model {model} long_context_threshold must be null or non-negative'
      )
    raw_tiers = _content_mapping(
      model_content['service_tiers'], f'price table model {model} service_tiers'
    )
    parsed_tiers: dict[str, ContextRates] = {}
    for tier, raw_context in raw_tiers.items():
      if not isinstance(tier, str) or tier == '':
        raise ValueError('price table service tier names must be non-empty strings')
      field = f'price table model {model} service tier {tier}'
      context = _content_mapping(raw_context, field)
      if set(context) != {'short', 'long'}:
        raise ValueError(f'{field} must contain short and long')
      parsed_tiers[tier] = ContextRates(
        short=_content_token_rates(context['short'], f'{field}.short'),
        long=None
        if context['long'] is None
        else _content_token_rates(context['long'], f'{field}.long'),
      )
    parsed_models[model] = ModelRates(MappingProxyType(parsed_tiers), threshold)
  return PriceTable(source, effective_date, MappingProxyType(parsed_models))


def _rates(
  input_rate: str, cached_input: Optional[str], cache_write: Optional[str], output: str
) -> TokenRates:
  """rates in the vendor page's column order, None for a blank cell."""
  return TokenRates(
    input=Decimal(input_rate),
    cached_input=None if cached_input is None else Decimal(cached_input),
    cache_write=None if cache_write is None else Decimal(cache_write),
    output=Decimal(output),
  )


# Rates are copied from the vendor page and updated by hand in a PR together
# with this date. Pricing never fetches vendor data at run time. The page's Fast
# tier is the one a response reports as `priority`.
PRICE_TABLE = PriceTable(
  source='https://developers.openai.com/api/docs/pricing',
  as_of=date(2026, 10, 12),
  models=MappingProxyType(
    {
      'gpt-6-astra': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('10.00', '1.00', '12.50', '50.00'),
              long=_rates('20.00', '2.00', '25.00', '75.00'),
            ),
            'flex': ContextRates(
              short=_rates('5.00', '0.50', '6.25', '25.00'),
              long=_rates('10.00', '1.00', '12.50', '37.50'),
            ),
            'priority': ContextRates(
              short=_rates('20.00', '2.00', '25.00', '100.00'),
              long=_rates('40.00', '4.00', '50.00', '150.00'),
            ),
          }
        ),
      ),
      'gpt-6.1-sol': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('2.00', '0.10', '2.50', '10.00'),
              long=_rates('4.00', '0.20', '5.00', '15.00'),
            ),
            'flex': ContextRates(
              short=_rates('1.00', '0.05', '1.25', '5.00'),
              long=_rates('2.00', '0.10', '2.50', '7.50'),
            ),
            'priority': ContextRates(
              short=_rates('4.00', '0.20', '5.00', '20.00'),
              long=_rates('8.00', '0.40', '10.00', '30.00'),
            ),
          }
        ),
      ),
      'gpt-6-luna': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('0.10', '0.01', '0.125', '0.50'),
              long=_rates('0.20', '0.02', '0.25', '0.75'),
            ),
            'flex': ContextRates(
              short=_rates('0.05', '0.005', '0.0625', '0.25'),
              long=_rates('0.10', '0.01', '0.125', '0.375'),
            ),
            'priority': ContextRates(
              short=_rates('0.20', '0.02', '0.25', '1.00'),
              long=_rates('0.40', '0.04', '0.50', '1.50'),
            ),
          }
        ),
      ),
      'gpt-6-sol': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('2.00', '0.20', '2.50', '10.00'),
              long=_rates('4.00', '0.40', '5.00', '15.00'),
            ),
            'flex': ContextRates(
              short=_rates('1.00', '0.10', '1.25', '5.00'),
              long=_rates('2.00', '0.20', '2.50', '7.50'),
            ),
            'priority': ContextRates(
              short=_rates('4.00', '0.40', '5.00', '20.00'),
              long=_rates('8.00', '0.80', '10.00', '30.00'),
            ),
          }
        ),
      ),
      'gpt-5.6-sol': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('4.00', '0.40', '5.00', '20.00'),
              long=_rates('8.00', '0.80', '10.00', '30.00'),
            ),
            'flex': ContextRates(
              short=_rates('2.00', '0.20', '2.50', '10.00'),
              long=_rates('4.00', '0.40', '5.00', '15.00'),
            ),
            'priority': ContextRates(
              short=_rates('8.00', '0.80', '10.00', '40.00'),
              long=_rates('16.00', '1.60', '20.00', '60.00'),
            ),
          }
        ),
      ),
      'gpt-5.6-terra': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('2.00', '0.20', '2.50', '12.00'),
              long=_rates('4.00', '0.40', '5.00', '18.00'),
            ),
            'flex': ContextRates(
              short=_rates('1.00', '0.10', '1.25', '6.00'),
              long=_rates('2.00', '0.20', '2.50', '9.00'),
            ),
            'priority': ContextRates(
              short=_rates('4.00', '0.40', '5.00', '24.00'),
              long=_rates('8.00', '0.80', '10.00', '36.00'),
            ),
          }
        ),
      ),
      'gpt-5.6-luna': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('0.20', '0.02', '0.25', '1.20'),
              long=_rates('0.40', '0.04', '0.50', '1.80'),
            ),
            'flex': ContextRates(
              short=_rates('0.10', '0.01', '0.125', '0.60'),
              long=_rates('0.20', '0.02', '0.25', '0.90'),
            ),
            'priority': ContextRates(
              short=_rates('0.40', '0.04', '0.50', '2.40'),
              long=_rates('0.80', '0.08', '1.00', '3.60'),
            ),
          }
        ),
      ),
      'gpt-5.5': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('5.00', '0.50', None, '30.00'),
              long=_rates('10.00', '1.00', None, '45.00'),
            ),
            'flex': ContextRates(
              short=_rates('2.50', '0.25', None, '15.00'),
              long=_rates('5.00', '0.50', None, '22.50'),
            ),
            'priority': ContextRates(_rates('12.50', '1.25', None, '75.00')),
          }
        ),
      ),
      'gpt-5.5-pro': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('30.00', None, None, '180.00'),
              long=_rates('60.00', None, None, '270.00'),
            ),
            'flex': ContextRates(_rates('15.00', None, None, '90.00')),
          }
        ),
      ),
      'gpt-5.4': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('2.50', '0.25', None, '15.00'),
              long=_rates('5.00', '0.50', None, '22.50'),
            ),
            'flex': ContextRates(
              short=_rates('1.25', '0.13', None, '7.50'),
              long=_rates('2.50', '0.25', None, '11.25'),
            ),
            'priority': ContextRates(_rates('5.00', '0.50', None, '30.00')),
          }
        ),
      ),
      'gpt-5.4-mini': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.75', '0.075', None, '4.50')),
            'flex': ContextRates(_rates('0.375', '0.0375', None, '2.25')),
            'priority': ContextRates(_rates('1.50', '0.15', None, '9.00')),
          }
        ),
      ),
      'gpt-5.4-nano': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.20', '0.02', None, '1.25')),
            'flex': ContextRates(_rates('0.10', '0.01', None, '0.625')),
          }
        ),
      ),
      'gpt-5.4-pro': ModelRates(
        long_context_threshold=_LONG_CONTEXT_THRESHOLD,
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(
              short=_rates('30.00', None, None, '180.00'),
              long=_rates('60.00', None, None, '270.00'),
            ),
            'flex': ContextRates(
              short=_rates('15.00', None, None, '90.00'),
              long=_rates('30.00', None, None, '135.00'),
            ),
          }
        ),
      ),
      'gpt-5.2': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.75', '0.175', None, '14.00')),
            'flex': ContextRates(_rates('0.875', '0.0875', None, '7.00')),
            'priority': ContextRates(_rates('3.50', '0.35', None, '28.00')),
          }
        ),
      ),
      'gpt-5.2-pro': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('21.00', None, None, '168.00')),
          }
        ),
      ),
      'gpt-5.1': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.25', '0.125', None, '10.00')),
            'flex': ContextRates(_rates('0.625', '0.0625', None, '5.00')),
            'priority': ContextRates(_rates('2.50', '0.25', None, '20.00')),
          }
        ),
      ),
      'gpt-5': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.25', '0.125', None, '10.00')),
            'flex': ContextRates(_rates('0.625', '0.0625', None, '5.00')),
            'priority': ContextRates(_rates('2.50', '0.25', None, '20.00')),
          }
        ),
      ),
      'gpt-5-mini': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.25', '0.025', None, '2.00')),
            'flex': ContextRates(_rates('0.125', '0.0125', None, '1.00')),
            'priority': ContextRates(_rates('0.45', '0.045', None, '3.60')),
          }
        ),
      ),
      'gpt-5-nano': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.05', '0.005', None, '0.40')),
            'flex': ContextRates(_rates('0.025', '0.0025', None, '0.20')),
          }
        ),
      ),
      'gpt-5-pro': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('15.00', None, None, '120.00')),
          }
        ),
      ),
      'gpt-4.1': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('2.00', '0.50', None, '8.00')),
            'priority': ContextRates(_rates('3.50', '0.875', None, '14.00')),
          }
        ),
      ),
      'gpt-4.1-mini': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.40', '0.10', None, '1.60')),
            'priority': ContextRates(_rates('0.70', '0.175', None, '2.80')),
          }
        ),
      ),
      'gpt-4.1-nano': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.10', '0.025', None, '0.40')),
            'priority': ContextRates(_rates('0.20', '0.05', None, '0.80')),
          }
        ),
      ),
      'gpt-4o': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('2.50', '1.25', None, '10.00')),
            'priority': ContextRates(_rates('4.25', '2.125', None, '17.00')),
          }
        ),
      ),
      'gpt-4o-2024-05-13': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('5.00', None, None, '15.00')),
            'priority': ContextRates(_rates('8.75', None, None, '26.25')),
          }
        ),
      ),
      'gpt-4o-mini': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.15', '0.075', None, '0.60')),
            'priority': ContextRates(_rates('0.25', '0.125', None, '1.00')),
          }
        ),
      ),
      'o1': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('15.00', '7.50', None, '60.00')),
          }
        ),
      ),
      'o1-pro': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('150.00', None, None, '600.00')),
          }
        ),
      ),
      'o3-pro': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('20.00', None, None, '80.00')),
          }
        ),
      ),
      'o3': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('2.00', '0.50', None, '8.00')),
            'flex': ContextRates(_rates('1.00', '0.25', None, '4.00')),
            'priority': ContextRates(_rates('3.50', '0.875', None, '14.00')),
          }
        ),
      ),
      'o4-mini': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.10', '0.275', None, '4.40')),
            'flex': ContextRates(_rates('0.55', '0.138', None, '2.20')),
            'priority': ContextRates(_rates('2.00', '0.50', None, '8.00')),
          }
        ),
      ),
      'o3-mini': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.10', '0.55', None, '4.40')),
          }
        ),
      ),
      'gpt-4-turbo-2024-04-09': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('10.00', None, None, '30.00')),
          }
        ),
      ),
      'gpt-4-0613': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('30.00', None, None, '60.00')),
          }
        ),
      ),
      'gpt-3.5-turbo': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.50', None, None, '1.50')),
          }
        ),
      ),
      'gpt-3.5-turbo-0125': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.50', None, None, '1.50')),
          }
        ),
      ),
      'gpt-3.5-turbo-1106': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.00', None, None, '2.00')),
          }
        ),
      ),
      'gpt-3.5-turbo-instruct': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.50', None, None, '2.00')),
          }
        ),
      ),
      'davinci-002': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('2.00', None, None, '2.00')),
          }
        ),
      ),
      'babbage-002': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('0.40', None, None, '0.40')),
          }
        ),
      ),
      'chat-latest': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('5.00', '0.50', None, '30.00')),
          }
        ),
      ),
      'gpt-5.3-codex': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.75', '0.175', None, '14.00')),
            'priority': ContextRates(_rates('3.50', '0.35', None, '28.00')),
          }
        ),
      ),
      'gpt-5-search-api': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('1.25', '0.125', None, '10.00')),
          }
        ),
      ),
      'gpt-5.6-cyber': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('12.50', '1.25', '15.625', '75.00')),
          }
        ),
      ),
      'gpt-5.5-cyber': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('12.50', '1.25', None, '75.00')),
          }
        ),
      ),
      'gpt-rosalind-research': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('5.00', '0.50', None, '25.00')),
          }
        ),
      ),
      'gpt-rosalind-discovery': ModelRates(
        service_tiers=MappingProxyType(
          {
            'standard': ContextRates(_rates('5.00', '0.50', None, '25.00')),
          }
        ),
      ),
    }
  ),
)
PRICE_TABLE_SHA256 = PRICE_TABLE.sha256


def _count(value: Any, field: str) -> int:
  if not isinstance(value, int) or isinstance(value, bool) or value < 0:
    raise ValueError(f'{field} must be a non-negative int')
  return value


_PRICED_SERVICE_TIERS: dict[Optional[str], str] = {
  None: 'standard',
  'auto': 'standard',
  'default': 'standard',
  'flex': 'flex',
  'fast': 'priority',
  'priority': 'priority',
}


def _billed(tokens: int, rate: Optional[Decimal], billing_class: str) -> Decimal:
  if rate is None:
    if tokens != 0:
      raise ValueError(f'{billing_class} tokens on a model that bills no {billing_class}')
    return Decimal(0)
  return Decimal(tokens) * rate


def price(
  model: str,
  usage: Mapping[str, Any],
  service_tier: Optional[str],
  table: Optional[PriceTable] = None,
) -> Optional[Decimal]:
  """Price one Responses API call from its raw usage and effective tier."""
  selected = PRICE_TABLE if table is None else table
  model_rates = selected.rates(model)
  tier = _PRICED_SERVICE_TIERS.get(service_tier)
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

  threshold = model_rates.long_context_threshold
  if threshold is not None and input_tokens > threshold:
    rates = context_rates.long
    if rates is None:
      return None
  else:
    rates = context_rates.short
  return (
    Decimal(fresh_tokens) * rates.input
    + _billed(cached_tokens, rates.cached_input, 'cached_input')
    + _billed(cache_write_tokens, rates.cache_write, 'cache_write')
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
class LLMSpec(llm_llm.LLMSpec):
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
  WIRE: ClassVar[str] = 'responses'
  selection_fields = MappingProxyType({'model': 'model', 'effort': 'reasoning_effort'})
  deferred_fields = frozenset({'effort'})
  effort_values = _VALID_REASONING_EFFORTS

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

  def request_options(self) -> dict:
    options: dict = {}
    if self.reasoning_effort is not None:
      options.update(
        reasoning={'effort': self.reasoning_effort, 'summary': 'auto'},
        include=['reasoning.encrypted_content'],
      )
    if self.service_tier is not None:
      options['service_tier'] = self.service_tier
    if self.compact_threshold is not None:
      options['context_management'] = [
        {'type': 'compaction', 'compact_threshold': self.compact_threshold}
      ]
    return options

  def is_transient(self, error: Exception) -> bool:
    from openai import APIConnectionError, APIStatusError

    return isinstance(error, APIConnectionError) or (
      isinstance(error, APIStatusError) and error.status_code >= 500
    )

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
