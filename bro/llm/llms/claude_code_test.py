import pytest

from bro.llm.llm import EFFORT_LEVELS, LLMSpec
from bro.llm.llms import claude_code


class TestSpec:
  def test_defaults_carry_the_sessions_model_and_effort(self):
    spec = claude_code.LLMSpec()
    assert (spec.model, spec.effort, spec.fast_mode) == (
      claude_code.DEFAULT_MODEL,
      claude_code.DEFAULT_EFFORT,
      False,
    )

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
