import dataclasses
import re
import subprocess
import sys
import warnings
from datetime import date
from decimal import Decimal
from typing import get_args

import pytest

import bro.llm.llm as llm_module
import bro.llm.llms.openai as openai_llm
from bro.base.source_root import SOURCE_ROOT
from bro.llm.llms.openai import LLMSpec
from bro.llm.pricing import StalePriceTableWarning


class TestLLMSpec:
  def test_default_spec_has_no_optional_knobs(self):
    spec = LLMSpec()
    assert spec.model == openai_llm.DEFAULT_MODEL
    assert spec.reasoning_effort is None
    assert spec.service_tier is None
    assert spec.compact_threshold is None

  def test_invalid_service_tier_rejected(self):
    with pytest.raises(ValueError, match='invalid service_tier'):
      LLMSpec(service_tier='nope')  # type: ignore[arg-type]

  def test_invalid_reasoning_effort_rejected(self):
    with pytest.raises(ValueError, match='invalid reasoning_effort'):
      LLMSpec(reasoning_effort='ludicrous')  # type: ignore[arg-type]

  @pytest.mark.parametrize('threshold', [0, -1])
  def test_invalid_compact_threshold_rejected(self, threshold: int):
    with pytest.raises(ValueError, match='invalid compact_threshold'):
      LLMSpec(compact_threshold=threshold)

  def test_fast_returns_new_spec_with_priority_tier(self):
    spec = LLMSpec(model='gpt-5.4-mini', reasoning_effort='medium', compact_threshold=50_000)
    fast = spec.fast()
    assert fast.service_tier == 'priority'
    # original untouched (frozen) and a distinct instance
    assert spec.service_tier is None
    assert fast is not spec
    # other fields preserved
    assert fast.model == 'gpt-5.4-mini'
    assert fast.reasoning_effort == 'medium'
    assert fast.compact_threshold == 50_000

  @pytest.mark.parametrize('level', ['low', 'medium', 'high', 'xhigh', 'max'])
  def test_with_effort_maps_shared_levels_through(self, level: str):
    assert LLMSpec().with_effort(level).reasoning_effort == level

  def test_with_effort_rejects_a_level_outside_the_neutral_vocabulary(self):
    # 'minimal' is a valid reasoning_effort but not a neutral level — with_effort
    # speaks only the neutral vocabulary
    with pytest.raises(ValueError, match='unknown effort level'):
      LLMSpec().with_effort('minimal')

  def test_with_effort_returns_new_spec_preserving_other_knobs(self):
    spec = LLMSpec(model='gpt-5.4-mini', service_tier='priority')
    with_effort = spec.with_effort('high')
    assert with_effort.reasoning_effort == 'high'
    # original untouched (frozen) and a distinct instance
    assert spec.reasoning_effort is None
    assert with_effort is not spec
    # other fields preserved — composes with fast()'s service_tier
    assert with_effort.model == 'gpt-5.4-mini'
    assert with_effort.service_tier == 'priority'

  def test_frozen_rejects_mutation(self):
    spec = LLMSpec()
    # frozen dataclass raises FrozenInstanceError, a subclass of AttributeError
    with pytest.raises(AttributeError):
      spec.service_tier = 'priority'  # type: ignore[misc]

  def test_dump_round_trips_through_base_from_dict(self):
    spec = LLMSpec(
      model='gpt-5.4-mini',
      reasoning_effort='medium',
      service_tier='priority',
      compact_threshold=50_000,
    )
    restored = llm_module.LLMSpec.from_dict(spec.dump())
    # frozen dataclass auto-generates __eq__ — single assertion covers every field
    assert restored == spec

  def test_dump_round_trip_handles_missing_optional_keys(self):
    # legacy / hand-written payloads may omit fields that were absent on write
    restored = llm_module.LLMSpec.from_dict({'type': 'openai', 'model': 'gpt-5'})
    assert isinstance(restored, LLMSpec)
    assert restored == LLMSpec(model='gpt-5')

  def test_from_dict_works_without_pre_importing_provider_module(self):
    # Run in a fresh interpreter so `bro.llm.llms.openai` is genuinely absent at
    # call time — simulates a process (e.g. an ad-hoc decisions_log reader)
    # that imports only `bro.llm.llm` and expects `from_dict` to still dispatch.
    # In-process monkeypatching would leave the dataclass class registered on
    # `LLMSpec.__subclasses__` even after sys.modules restoration.
    import subprocess
    import sys

    from bro.base.source_root import SOURCE_ROOT

    script = (
      'import sys; '
      "assert 'bro.llm.llms.openai' not in sys.modules; "
      'from bro.llm.llm import LLMSpec; '
      "spec = LLMSpec.from_dict({'type': 'openai', 'model': 'gpt-5'}); "
      "assert spec.model == 'gpt-5'; "
      "assert spec.TYPE == 'openai'"
    )
    result = subprocess.run(
      [sys.executable, '-c', script], capture_output=True, text=True, cwd=SOURCE_ROOT.parent
    )
    assert result.returncode == 0, f'stderr: {result.stderr}'

  def test_reasoning_effort_values_match_openai(self):
    # the module mirrors openai's ReasoningEffort values locally so spec
    # validation needs no openai import; catch the mirror drifting on SDK bumps

    import openai.types.shared

    openai_values = get_args(get_args(openai.types.shared.ReasoningEffort)[0])
    assert frozenset(get_args(openai_llm.ReasoningEffort)) == frozenset(openai_values)

  def test_resolving_recipe_does_not_import_native_engine_or_sdk(self):
    script = (
      'import sys; '
      'from bro.llm import providers; '
      "spec = providers.default_spec('openai'); "
      "assert spec.TYPE == 'openai'; "
      "assert 'bro.llm.mcp' not in sys.modules; "
      "assert not any(name == 'bro.native' or name.startswith('bro.native.') for name in sys.modules); "
      "assert 'openai' not in sys.modules"
    )
    result = subprocess.run(
      [sys.executable, '-c', script], capture_output=True, text=True, cwd=SOURCE_ROOT.parent
    )
    assert result.returncode == 0, f'stderr: {result.stderr}'

  def test_importing_module_does_not_import_openai(self):
    # every bro module constructs an LLMSpec at class-definition time, so the
    # spec side must stay decoupled from the heavyweight openai package. Fresh
    # interpreter: in-process, other tests would already have openai loaded.
    import subprocess
    import sys

    from bro.base.source_root import SOURCE_ROOT

    script = (
      'import sys; '
      'import bro.llm.llms.openai; '
      "bro.llm.llms.openai.LLMSpec(reasoning_effort='medium'); "
      "assert 'openai' not in sys.modules"
    )
    result = subprocess.run(
      [sys.executable, '-c', script], capture_output=True, text=True, cwd=SOURCE_ROOT.parent
    )
    assert result.returncode == 0, f'stderr: {result.stderr}'


class TestPricing:
  def test_published_standard_and_priority_rates_price_raw_usage(self):
    usage = {
      'input_tokens': 10,
      'input_tokens_details': {'cached_tokens': 3, 'cache_write_tokens': 2},
      'output_tokens': 4,
    }

    assert openai_llm.price('gpt-5.6-terra', usage, 'default') == Decimal('0.0000636')
    assert openai_llm.price('gpt-5.6-terra', usage, 'priority') == Decimal('0.0001272')

  def test_published_long_context_rates_start_above_272k(self):
    assert openai_llm.price(
      'gpt-5.6-terra', {'input_tokens': 272_000, 'output_tokens': 0}, None
    ) == Decimal('0.544')
    assert openai_llm.price(
      'gpt-5.6-terra', {'input_tokens': 272_001, 'output_tokens': 0}, None
    ) == Decimal('1.088004')

  def test_an_unknown_model_or_unpriced_tier_is_not_treated_as_free(self):
    usage = {'input_tokens': 1, 'output_tokens': 1}
    assert openai_llm.price('unknown', usage, None) is None
    assert openai_llm.price('gpt-5.6-terra', usage, 'flex') is None

  @pytest.mark.parametrize(
    'usage',
    [
      {'input_tokens': -1, 'output_tokens': 0},
      {'input_tokens': True, 'output_tokens': 0},
      {'input_tokens': 1, 'output_tokens': 1.5},
      {
        'input_tokens': 1,
        'input_tokens_details': {'cached_tokens': 2},
        'output_tokens': 0,
      },
    ],
  )
  def test_malformed_usage_is_rejected(self, usage):
    with pytest.raises(ValueError):
      openai_llm.price('gpt-5.6-terra', usage, None)

  def test_a_caller_can_supply_a_price_table(self):
    current_model = openai_llm.PRICE_TABLE.models['gpt-5.6-terra']
    current_standard = current_model.service_tiers['standard']
    custom_standard = dataclasses.replace(
      current_standard,
      short=dataclasses.replace(
        current_standard.short,
        input=Decimal('100.00'),
      ),
    )
    custom_model = dataclasses.replace(
      current_model,
      service_tiers={**current_model.service_tiers, 'standard': custom_standard},
    )
    custom_table = dataclasses.replace(
      openai_llm.PRICE_TABLE,
      models={'gpt-5.6-terra': custom_model},
    )

    assert openai_llm.price(
      'gpt-5.6-terra', {'input_tokens': 1, 'output_tokens': 0}, None, custom_table
    ) == Decimal('0.0001')

  def test_table_metadata_carries_a_stable_content_digest(self):
    assert openai_llm.PRICE_TABLE.source == 'https://developers.openai.com/api/docs/pricing'
    assert isinstance(openai_llm.PRICE_TABLE.as_of, date)
    assert openai_llm.PRICE_TABLE.as_of <= date.today()
    assert openai_llm.PRICE_TABLE_SHA256 == openai_llm.PRICE_TABLE.sha256
    assert re.fullmatch(r'[0-9a-f]{64}', openai_llm.PRICE_TABLE_SHA256)

  def test_a_stale_table_warns_only_once_for_its_provider_and_date(self):
    stale = dataclasses.replace(openai_llm.PRICE_TABLE, as_of=date(2020, 1, 2))
    usage = {'input_tokens': 1, 'output_tokens': 0}

    with pytest.warns(StalePriceTableWarning, match='openai.*2020-01-02'):
      openai_llm.price('gpt-5.6-terra', usage, None, stale)
    with warnings.catch_warnings(record=True) as caught:
      warnings.simplefilter('always')
      openai_llm.price('gpt-5.6-terra', usage, None, stale)
    assert caught == []
