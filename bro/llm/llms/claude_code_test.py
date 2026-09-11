import re
from datetime import date
from decimal import Decimal

import pytest

from bro.llm.llm import EFFORT_LEVELS, LLMSpec
from bro.llm.llms import claude_code


class TestSpec:
  def test_defaults_carry_the_sessions_model_and_effort(self):
    spec = claude_code.LLMSpec()
    assert (spec.model, spec.effort) == (claude_code.DEFAULT_MODEL, claude_code.DEFAULT_EFFORT)

  def test_accepts_every_neutral_effort_level(self):
    # `claude --effort` takes the neutral vocabulary unmapped, so a level the
    # flags accept must never fail at spec build
    for level in EFFORT_LEVELS:
      assert claude_code.LLMSpec().with_effort(level).effort == level

  def test_rejects_an_effort_outside_the_vocabulary(self):
    with pytest.raises(ValueError, match='invalid effort'):
      claude_code.LLMSpec(effort='ludicrous')

  def test_fast_sets_the_session_knob_without_mutating(self):
    spec = claude_code.LLMSpec()
    assert spec.fast().fast_mode is True
    assert spec.fast_mode is False

  def test_round_trips_through_the_discriminator(self):
    spec = claude_code.LLMSpec(model='claude-fable-5', effort='max', fast_mode=True)
    assert LLMSpec.from_dict(spec.dump()) == spec

  def test_needs_no_llm_key_of_its_own(self):
    # a claude session authenticates through its own surface secret, not a key
    # the framework reads to drive an API
    assert claude_code.LLMSpec().needed_secrets() == ()


class TestPricing:
  @pytest.mark.parametrize(
    'model,expected',
    [
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

    assert claude_code.price(model, usage, None) == expected

  def test_an_unknown_model_is_not_treated_as_free(self):
    assert claude_code.price('unknown', {'input_tokens': 1, 'output_tokens': 1}, None) is None

  def test_a_cache_write_requires_its_ttl_breakdown(self):
    usage = {'input_tokens': 1, 'cache_creation_input_tokens': 1, 'output_tokens': 1}
    with pytest.raises(ValueError, match='cache_creation is required'):
      claude_code.price('claude-opus-5', usage, None)

  def test_the_ttl_breakdown_must_equal_total_cache_creation(self):
    usage = {
      'input_tokens': 1,
      'cache_creation_input_tokens': 3,
      'cache_creation': {'ephemeral_5m_input_tokens': 1},
      'output_tokens': 1,
    }
    with pytest.raises(ValueError, match='does not equal'):
      claude_code.price('claude-opus-5', usage, None)

  def test_a_serialized_price_snapshot_can_be_reconstructed(self):
    current = claude_code.PRICE_TABLE
    model = current.models['claude-opus-5']
    snapshot = claude_code.price_table_from_content(
      current.source,
      current.as_of.isoformat(),
      {'claude-opus-5': model.content()},
    )
    usage = {'input_tokens': 1, 'output_tokens': 1}

    assert claude_code.price('claude-opus-5', usage, None, snapshot) == claude_code.price(
      'claude-opus-5', usage, None, current
    )

  def test_table_metadata_carries_a_stable_content_digest(self):
    assert (
      claude_code.PRICE_TABLE.source == 'https://platform.claude.com/docs/en/about-claude/pricing'
    )
    assert isinstance(claude_code.PRICE_TABLE.as_of, date)
    assert claude_code.PRICE_TABLE.as_of <= date.today()
    assert claude_code.PRICE_TABLE_SHA256 == claude_code.PRICE_TABLE.sha256
    assert re.fullmatch(r'[0-9a-f]{64}', claude_code.PRICE_TABLE_SHA256)
