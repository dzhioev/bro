import re
from datetime import date
from decimal import Decimal

import pytest

from bro.anthropic import declaration as anthropic
from bro.llm.llm import EFFORT_LEVELS, Recipe


def _rates(value: str) -> dict[str, str]:
  return {
    'input': value,
    'cache_write_5m': value,
    'cache_write_1h': value,
    'cache_read': value,
    'output': value,
  }


class TestSpec:
  def test_accepts_every_neutral_effort_level(self):
    # `claude --effort` takes the neutral vocabulary unmapped, so a level the
    # flags accept must never fail at spec build
    for level in EFFORT_LEVELS:
      assert anthropic.Recipe().with_effort(level).effort == level

  def test_rejects_an_effort_outside_the_vocabulary(self):
    with pytest.raises(ValueError, match='invalid effort'):
      Recipe.from_dict(
        {'type': 'anthropic', 'model': anthropic.DEFAULT_MODEL, 'effort': 'ludicrous'}
      )

  def test_fast_sets_the_session_knob_without_mutating(self):
    spec = anthropic.Recipe()
    assert spec.fast().fast_mode is True
    assert spec.fast_mode is False

  def test_round_trips_through_the_discriminator(self):
    spec = anthropic.Recipe(model='claude-fable-5', effort='max', fast_mode=True)
    assert Recipe.from_dict(spec.dump()) == spec

  def test_round_trips_a_recipe_naming_no_model(self):
    spec = anthropic.Recipe(model=None, effort=None)
    assert Recipe.from_dict(spec.dump()) == spec

  def test_the_default_model_is_a_listed_id(self):
    assert anthropic.DEFAULT_MODEL in anthropic.MODELS.values()

  def test_needs_no_llm_key_of_its_own(self):
    # a claude session authenticates through its own surface secret, not a key
    # the framework reads to drive an API
    assert anthropic.Recipe().needed_secrets() == ()


class TestPricing:
  @pytest.mark.parametrize(
    'model,expected',
    [
      ('claude-opus-5-5', Decimal('0.0000372')),
      ('claude-opus-5', Decimal('0.00004675')),
      ('claude-sonnet-5', Decimal('0.0000187')),
      ('claude-fable-5', Decimal('0.0000935')),
      ('claude-fable-5-1', Decimal('0.00009275')),
      ('claude-haiku-4-5-20251001', Decimal('0.00000935')),
    ],
  )
  def test_published_model_rates_price_each_transcript_usage_class(self, model, expected):
    usage = {
      'input_tokens': 1,
      'cache_creation_input_tokens': 2,
      'cache_creation': {
        'ephemeral_5m_input_tokens': 1,
        'ephemeral_1h_input_tokens': 1,
      },
      'cache_read_input_tokens': 1,
      'output_tokens': 1,
    }

    assert anthropic.price(model, usage, None) == expected

  def test_an_unknown_model_is_not_treated_as_free(self):
    assert anthropic.price('unknown', {'input_tokens': 1, 'output_tokens': 1}, None) is None

  def test_a_cache_write_requires_its_ttl_breakdown(self):
    usage = {'input_tokens': 1, 'cache_creation_input_tokens': 1, 'output_tokens': 1}
    with pytest.raises(ValueError, match='cache_creation is required'):
      anthropic.price('claude-opus-5', usage, None)

  def test_the_ttl_breakdown_must_equal_total_cache_creation(self):
    usage = {
      'input_tokens': 1,
      'cache_creation_input_tokens': 3,
      'cache_creation': {'ephemeral_5m_input_tokens': 1},
      'output_tokens': 1,
    }
    with pytest.raises(ValueError, match='does not equal'):
      anthropic.price('claude-opus-5', usage, None)

  def test_a_serialized_price_snapshot_can_be_reconstructed(self):
    current = anthropic.PRICE_TABLE
    model = current.models['claude-opus-5']
    snapshot = anthropic.price_table_from_content(
      current.source,
      current.as_of.isoformat(),
      {'claude-opus-5': model.content()},
    )
    usage = {'input_tokens': 1, 'output_tokens': 1}

    assert anthropic.price('claude-opus-5', usage, None, snapshot) == anthropic.price(
      'claude-opus-5', usage, None, current
    )

  def test_table_metadata_carries_a_stable_content_digest(self):
    assert (
      anthropic.PRICE_TABLE.source == 'https://platform.claude.com/docs/en/about-claude/pricing'
    )
    assert isinstance(anthropic.PRICE_TABLE.as_of, date)
    assert anthropic.PRICE_TABLE.as_of <= date.today()
    assert anthropic.PRICE_TABLE_SHA256 == anthropic.PRICE_TABLE.sha256
    assert re.fullmatch(r'[0-9a-f]{64}', anthropic.PRICE_TABLE_SHA256)


class TestLongContextPricing:
  # the serialized shape retention manifests persist (`benchmark/bro/benchmark/retention.py`)
  TABLE = anthropic.price_table_from_content(
    'https://example.com/pricing',
    date.today().isoformat(),
    {
      'flat': _rates('1'),
      'tiered': {**_rates('1'), 'long_context': {'threshold': 10, 'rates': _rates('2')}},
    },
  )

  @staticmethod
  def _usage(*, input_tokens: int, cache_write: int, cache_read: int) -> dict:
    return {
      'input_tokens': input_tokens,
      'cache_creation_input_tokens': cache_write,
      'cache_creation': {'ephemeral_5m_input_tokens': cache_write},
      'cache_read_input_tokens': cache_read,
      'output_tokens': 5,
    }

  def test_a_prompt_over_the_threshold_pays_the_long_rates_on_every_class(self):
    usage = self._usage(input_tokens=4, cache_write=3, cache_read=4)
    assert anthropic.price('tiered', usage, None, self.TABLE) == Decimal(32) / 1_000_000

  def test_a_prompt_at_the_threshold_pays_the_base_rates(self):
    usage = self._usage(input_tokens=4, cache_write=3, cache_read=3)
    assert anthropic.price('tiered', usage, None, self.TABLE) == Decimal(15) / 1_000_000

  def test_a_model_without_a_tier_pays_its_rates_at_any_length(self):
    usage = self._usage(input_tokens=4, cache_write=3, cache_read=4)
    assert anthropic.price('flat', usage, None, self.TABLE) == Decimal(16) / 1_000_000

  def test_a_tiered_model_survives_a_serialized_snapshot(self):
    current = anthropic.PRICE_TABLE
    snapshot = anthropic.price_table_from_content(
      current.source,
      current.as_of.isoformat(),
      {'claude-haiku-5-5': current.models['claude-haiku-5-5'].content()},
    )
    usage = {'input_tokens': 100_001, 'output_tokens': 1}

    assert anthropic.price('claude-haiku-5-5', usage, None, snapshot) == anthropic.price(
      'claude-haiku-5-5', usage, None, current
    )

  def test_a_long_context_tier_requires_its_threshold(self):
    tier = {'rates': _rates('2')}
    with pytest.raises(ValueError, match='threshold and rates'):
      anthropic.price_table_from_content(
        'https://example.com', '2026-01-01', {'model': {**_rates('1'), 'long_context': tier}}
      )


class TestFastModePricing:
  # the serialized shape retention manifests persist (`benchmark/bro/benchmark/retention.py`)
  TABLE = anthropic.price_table_from_content(
    'https://example.com/pricing',
    date.today().isoformat(),
    {
      'standard-only': _rates('1'),
      'fast': {**_rates('1'), 'fast': _rates('3')},
      'tiered-fast': {
        **_rates('1'),
        'long_context': {'threshold': 10, 'rates': _rates('2')},
        'fast': _rates('3'),
      },
    },
  )

  @staticmethod
  def _usage(*, input_tokens: int = 4, **speed: str) -> dict:
    return {
      'input_tokens': input_tokens,
      'cache_creation_input_tokens': 3,
      'cache_creation': {'ephemeral_5m_input_tokens': 1, 'ephemeral_1h_input_tokens': 2},
      'cache_read_input_tokens': 2,
      'output_tokens': 5,
      **speed,
    }

  def test_a_fast_call_pays_the_fast_rates_on_every_class(self):
    usage = self._usage(speed='fast')
    assert anthropic.price('fast', usage, None, self.TABLE) == Decimal(42) / 1_000_000

  @pytest.mark.parametrize('speed', [{'speed': 'standard'}, {}])
  def test_a_standard_or_unmarked_call_pays_the_standard_rates(self, speed):
    usage = self._usage(**speed)
    assert anthropic.price('fast', usage, None, self.TABLE) == Decimal(14) / 1_000_000

  def test_fast_rates_apply_over_the_long_context_threshold(self):
    usage = self._usage(input_tokens=6, speed='fast')
    assert anthropic.price('tiered-fast', usage, None, self.TABLE) == Decimal(48) / 1_000_000

  @pytest.mark.parametrize('model,speed', [('standard-only', 'fast'), ('fast', 'turbo')])
  def test_a_speed_without_published_rates_is_not_treated_as_free(self, model, speed):
    assert anthropic.price(model, self._usage(speed=speed), None, self.TABLE) is None

  def test_fast_rates_survive_a_serialized_snapshot(self):
    current = anthropic.PRICE_TABLE
    snapshot = anthropic.price_table_from_content(
      current.source,
      current.as_of.isoformat(),
      {'claude-opus-5-5': current.models['claude-opus-5-5'].content()},
    )
    usage = self._usage(speed='fast')

    assert anthropic.price('claude-opus-5-5', usage, None, snapshot) == anthropic.price(
      'claude-opus-5-5', usage, None, current
    )
