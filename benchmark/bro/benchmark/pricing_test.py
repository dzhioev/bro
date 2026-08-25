from datetime import date
from decimal import Decimal

import pytest

from bro.benchmark.pricing import (
  PRICE_TABLE,
  UnpricedModelError,
  call_cost_usd,
  optional_call_cost_usd,
)

MODEL = 'gpt-5.6-terra'


def test_the_roster_model_carries_verified_standard_prices():
  price = PRICE_TABLE[MODEL]

  assert price.source == 'https://developers.openai.com/api/docs/pricing'
  assert price.as_of == date(2026, 8, 25)
  assert price.long_context_threshold == 272_000
  assert price.service_tier == 'standard'
  assert price.short_context.input == Decimal('2.00')
  assert price.short_context.cache_write == Decimal('2.50')
  assert price.short_context.cache_read == Decimal('0.20')
  assert price.short_context.output == Decimal('12.00')
  assert price.long_context.input == Decimal('4.00')
  assert price.long_context.cache_write == Decimal('5.00')
  assert price.long_context.cache_read == Decimal('0.40')
  assert price.long_context.output == Decimal('18.00')


def test_each_billed_class_contributes_to_a_short_context_call():
  cost = call_cost_usd(
    MODEL,
    {'input': 5, 'cache_write': 2, 'cache_read': 3, 'output': 4},
  )

  assert cost == Decimal('0.0000636')


def test_long_context_prices_apply_only_above_the_published_threshold():
  assert call_cost_usd(MODEL, {'input': 272_000}) == Decimal('0.544')
  assert call_cost_usd(MODEL, {'input': 272_001}) == Decimal('1.088004')


def test_an_unpriced_model_is_not_treated_as_free():
  with pytest.raises(UnpricedModelError, match="no benchmark price for model 'unknown'"):
    call_cost_usd('unknown', {'input': 1})

  assert optional_call_cost_usd('unknown', {'input': 1}) is None


@pytest.mark.parametrize(
  'counts',
  [
    {'input': -1},
    {'input': True},
    {'input': 1.5},
    {'other': 1},
  ],
)
def test_malformed_counts_fail_before_pricing(counts):
  with pytest.raises(ValueError, match='token count|unknown billed token classes'):
    call_cost_usd(MODEL, counts)
