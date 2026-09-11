"""Claude Code as an LLM provider: the model and knobs a claude session runs
under, carried as an `LLMSpec` like every other provider's recipe.

Claude Code drives its own agent loop, so this is a recipe and nothing more — it
builds no client, and a session surface reads the fields off it.
"""

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, ClassVar, Optional, Self

import bro.llm.llm as llm_llm
from bro.llm import pricing

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

_ONE_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenRates:
  """USD per million Anthropic tokens, in transcript usage classes."""

  input: Decimal
  cache_write_5m: Decimal
  cache_write_1h: Decimal
  cache_read: Decimal
  output: Decimal

  def content(self) -> dict[str, str]:
    return pricing.decimal_strings(
      {
        'input': self.input,
        'cache_write_5m': self.cache_write_5m,
        'cache_write_1h': self.cache_write_1h,
        'cache_read': self.cache_read,
        'output': self.output,
      }
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


def price_table_from_content(source: str, as_of: str, models: Mapping[str, Any]) -> PriceTable:
  """Reconstruct an immutable table from this provider's serialized vocabulary."""
  if source == '' or source.strip() != source:
    raise ValueError('price table source must be a non-empty trimmed string')
  try:
    effective_date = date.fromisoformat(as_of)
  except ValueError as error:
    raise ValueError('price table as_of must be an ISO date') from error
  expected = {'input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output'}
  parsed_models: dict[str, TokenRates] = {}
  for model, raw_rates in models.items():
    if not isinstance(model, str) or model == '':
      raise ValueError('price table model names must be non-empty strings')
    rates = _content_mapping(raw_rates, f'price table model {model}')
    if set(rates) != expected:
      raise ValueError(f'price table model {model} must contain exactly {sorted(expected)}')
    parsed_models[model] = TokenRates(
      input=_content_decimal(rates['input'], f'price table model {model}.input'),
      cache_write_5m=_content_decimal(
        rates['cache_write_5m'], f'price table model {model}.cache_write_5m'
      ),
      cache_write_1h=_content_decimal(
        rates['cache_write_1h'], f'price table model {model}.cache_write_1h'
      ),
      cache_read=_content_decimal(rates['cache_read'], f'price table model {model}.cache_read'),
      output=_content_decimal(rates['output'], f'price table model {model}.output'),
    )
  return PriceTable(source, effective_date, MappingProxyType(parsed_models))


# Rates are copied from the vendor page and updated by hand in a PR together
# with this date. Pricing never fetches vendor data at run time.
PRICE_TABLE = PriceTable(
  source='https://platform.claude.com/docs/en/about-claude/pricing',
  as_of=date(2026, 9, 11),
  models=MappingProxyType(
    {
      'claude-opus-5': TokenRates(
        input=Decimal('5.00'),
        cache_write_5m=Decimal('6.25'),
        cache_write_1h=Decimal('10.00'),
        cache_read=Decimal('0.50'),
        output=Decimal('25.00'),
      ),
      'claude-sonnet-5': TokenRates(
        input=Decimal('2.00'),
        cache_write_5m=Decimal('2.50'),
        cache_write_1h=Decimal('4.00'),
        cache_read=Decimal('0.20'),
        output=Decimal('10.00'),
      ),
      'claude-fable-5': TokenRates(
        input=Decimal('10.00'),
        cache_write_5m=Decimal('12.50'),
        cache_write_1h=Decimal('20.00'),
        cache_read=Decimal('1.00'),
        output=Decimal('50.00'),
      ),
      'claude-fable-5-1': TokenRates(
        input=Decimal('10.00'),
        cache_write_5m=Decimal('12.50'),
        cache_write_1h=Decimal('20.00'),
        cache_read=Decimal('0.25'),
        output=Decimal('50.00'),
      ),
      'claude-haiku-4-5-20251001': TokenRates(
        input=Decimal('1.00'),
        cache_write_5m=Decimal('1.25'),
        cache_write_1h=Decimal('2.00'),
        cache_read=Decimal('0.10'),
        output=Decimal('5.00'),
      ),
    }
  ),
)
PRICE_TABLE_SHA256 = PRICE_TABLE.sha256


def _count(value: Any, field: str) -> int:
  if not isinstance(value, int) or isinstance(value, bool) or value < 0:
    raise ValueError(f'{field} must be a non-negative int')
  return value


def price(
  model: str,
  usage: Mapping[str, Any],
  service_tier: Optional[str],
  table: Optional[PriceTable] = None,
) -> Optional[Decimal]:
  """Price one Anthropic transcript call from its raw usage record."""
  if service_tier is not None:
    raise ValueError('claude-code usage has no service tier')
  selected = PRICE_TABLE if table is None else table
  rates = selected.models.get(model)
  if rates is None:
    return None
  pricing.warn_if_stale('claude-code', selected.as_of)

  input_tokens = _count(usage.get('input_tokens'), 'input_tokens')
  cache_creation_tokens = _count(
    usage.get('cache_creation_input_tokens', 0), 'cache_creation_input_tokens'
  )
  cache_read_tokens = _count(usage.get('cache_read_input_tokens', 0), 'cache_read_input_tokens')
  output_tokens = _count(usage.get('output_tokens'), 'output_tokens')
  raw_creation = usage.get('cache_creation')
  if raw_creation is None:
    if cache_creation_tokens != 0:
      raise ValueError('cache_creation is required when cache_creation_input_tokens is nonzero')
    cache_write_5m_tokens = 0
    cache_write_1h_tokens = 0
  else:
    if not isinstance(raw_creation, Mapping):
      raise ValueError('cache_creation must be an object')
    cache_write_5m_tokens = _count(
      raw_creation.get('ephemeral_5m_input_tokens', 0),
      'cache_creation.ephemeral_5m_input_tokens',
    )
    cache_write_1h_tokens = _count(
      raw_creation.get('ephemeral_1h_input_tokens', 0),
      'cache_creation.ephemeral_1h_input_tokens',
    )
    if cache_write_5m_tokens + cache_write_1h_tokens != cache_creation_tokens:
      raise ValueError('cache_creation breakdown does not equal cache_creation_input_tokens')

  return (
    Decimal(input_tokens) * rates.input
    + Decimal(cache_write_5m_tokens) * rates.cache_write_5m
    + Decimal(cache_write_1h_tokens) * rates.cache_write_1h
    + Decimal(cache_read_tokens) * rates.cache_read
    + Decimal(output_tokens) * rates.output
  ) / _ONE_MILLION


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
