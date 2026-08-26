import pytest

from bro.llm import providers
from bro.llm.llm import FAILURE_CATEGORIES, FailureSignature, NativeLLMSpec
from bro.llm.llms import claude_code, echo, openai as openai_llm


class TestRoster:
  def test_every_provider_declares_the_roster_contract(self):
    for name in providers.known_names():
      assert isinstance(providers.default_spec(name), providers.LLMSpec)
      assert isinstance(providers.models(name), dict)

  def test_a_provider_name_is_its_specs_type(self):
    for name in providers.known_names():
      assert providers.default_spec(name).TYPE == name

  def test_short_names_do_not_collide_across_providers(self):
    # a `--model` value with no `--provider` is resolved by whichever roster
    # carries it, so one name may never be served by two
    seen: dict[str, str] = {}
    for provider in providers.known_names():
      for short_name in providers.models(provider):
        assert short_name not in seen, f'{short_name} in both {seen.get(short_name)} and {provider}'
        seen[short_name] = provider

  def test_unknown_provider_names_the_known_ones(self):
    with pytest.raises(providers.LLMSelectionError, match='openai'):
      providers.default_spec('nope')


class TestModelResolution:
  def test_short_name_resolves_within_its_provider(self):
    assert providers.resolve_model('openai', 'sol') == 'gpt-5.6-sol'
    assert providers.resolve_model('claude-code', 'fable5') == 'claude-fable-5'

  def test_an_unlisted_model_passes_through(self):
    assert providers.resolve_model('openai', 'gpt-6-unreleased') == 'gpt-6-unreleased'

  def test_provider_inferred_from_a_short_name(self):
    assert providers.provider_of_model('terra') == 'openai'
    assert providers.provider_of_model('opus5') == 'claude-code'

  def test_provider_inferred_from_a_full_id(self):
    assert providers.provider_of_model('claude-opus-5') == 'claude-code'

  def test_an_unknown_model_lists_the_short_names(self):
    with pytest.raises(providers.LLMSelectionError, match='name its provider'):
      providers.provider_of_model('gpt-6-unreleased')


class TestParsing:
  @pytest.mark.parametrize(
    'value,expected',
    [
      ('openai:sol:max+fast', providers.LLMSelection('openai', 'sol', 'max', True)),
      ('::high', providers.LLMSelection(effort='high')),
      (':fable5', providers.LLMSelection(model='fable5')),
      ('claude-code::high', providers.LLMSelection(provider='claude-code', effort='high')),
      ('openai', providers.LLMSelection(provider='openai')),
      ('+fast', providers.LLMSelection(fast=True)),
      (':fable5:max', providers.LLMSelection(model='fable5', effort='max')),
    ],
  )
  def test_round_trips_through_the_canonical_form(self, value, expected):
    parsed = providers.parse(value)
    assert parsed == expected
    assert parsed.format() == value

  @pytest.mark.parametrize(
    'value', ['', 'nope', ':nosuchmodel', '::ludicrous', 'a:b:c:d', 'openai+slow', '+fast+fast']
  )
  def test_malformed_values_are_rejected(self, value):
    with pytest.raises(providers.LLMSelectionError):
      providers.parse(value)


class TestResolve:
  def test_an_empty_selection_leaves_the_base(self):
    base = openai_llm.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
    assert providers.resolve(base, providers.LLMSelection()) == base

  def test_a_model_alone_keeps_the_bases_other_knobs(self):
    base = openai_llm.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
    resolved = providers.resolve(base, providers.parse(':terra'))
    assert resolved == openai_llm.LLMSpec(model='gpt-5.6-terra', reasoning_effort='high')

  def test_a_provider_selects_its_own_default_recipe(self):
    base = openai_llm.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
    resolved = providers.resolve(base, providers.parse('openai'))
    assert resolved == openai_llm.LLMSpec()

  def test_a_model_of_another_provider_switches_recipes(self):
    base = openai_llm.LLMSpec(model='gpt-5.6-sol', reasoning_effort='high')
    resolved = providers.resolve(base, providers.parse(':fable5'))
    assert resolved == claude_code.LLMSpec(model='claude-fable-5')

  def test_knobs_apply_over_whichever_recipe_was_selected(self):
    resolved = providers.resolve(
      openai_llm.LLMSpec(), providers.parse('claude-code:sonnet5:max+fast')
    )
    assert resolved == claude_code.LLMSpec(model='claude-sonnet-5', effort='max', fast_mode=True)

  def test_fast_falls_back_on_a_provider_without_it(self):
    assert providers.resolve(echo.LLMSpec(), providers.parse('+fast')) == echo.LLMSpec()

  def test_an_effort_override_raises_on_a_provider_without_it(self):
    with pytest.raises(NotImplementedError, match='effort override'):
      providers.resolve(echo.LLMSpec(), providers.parse('::high'))


class TestNativeSplit:
  def test_claude_code_is_a_recipe_the_native_loop_cannot_run(self):
    assert not isinstance(claude_code.LLMSpec(), NativeLLMSpec)

  def test_the_api_providers_build_clients(self):
    assert isinstance(openai_llm.LLMSpec(), NativeLLMSpec)
    assert isinstance(echo.LLMSpec(), NativeLLMSpec)

  def test_every_provider_round_trips_through_from_dict(self):
    for name in providers.known_names():
      spec = providers.default_spec(name)
      assert providers.LLMSpec.from_dict(spec.dump()) == spec


class TestFailureSignatures:
  def test_every_provider_declares_its_signatures(self):
    for name in providers.known_names():
      for signature in providers.failure_signatures(name):
        assert signature.category in FAILURE_CATEGORIES

  def test_signatures_reach_the_providers_declaration(self):
    assert providers.failure_signatures('openai') == openai_llm.FAILURE_SIGNATURES

  def test_a_signature_off_the_category_vocabulary_is_refused(self):
    with pytest.raises(ValueError, match='unknown failure category'):
      FailureSignature(pattern='x', category='sprint')
