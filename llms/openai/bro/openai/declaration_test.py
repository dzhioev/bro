import dataclasses
import re
import subprocess
import sys
import warnings
from datetime import date
from decimal import Decimal
from typing import Optional, get_args

import pytest

import bro.llm.llm as llm_module
import bro.openai.declaration as openai_llm
from bro.base.source_root import SOURCE_ROOT
from bro.llm.pricing import StalePriceTableWarning
from bro.openai.declaration import Recipe


class TestRecipe:
  def test_default_spec_has_no_optional_knobs(self):
    spec = Recipe()
    assert spec.model == openai_llm.DEFAULT_MODEL
    assert spec.reasoning_effort is None
    assert spec.service_tier is None
    assert spec.compact_threshold is None

  def test_invalid_service_tier_rejected(self):
    with pytest.raises(ValueError, match='invalid service_tier'):
      Recipe(service_tier='nope')  # type: ignore[arg-type]

  def test_invalid_reasoning_effort_rejected(self):
    with pytest.raises(ValueError, match='invalid reasoning_effort'):
      Recipe(reasoning_effort='ludicrous')  # type: ignore[arg-type]

  @pytest.mark.parametrize('threshold', [0, -1])
  def test_invalid_compact_threshold_rejected(self, threshold: int):
    with pytest.raises(ValueError, match='invalid compact_threshold'):
      Recipe(compact_threshold=threshold)

  def test_fast_returns_new_spec_with_priority_tier(self):
    spec = Recipe(model='gpt-5.4-mini', reasoning_effort='medium', compact_threshold=50_000)
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
    assert Recipe().with_effort(level).reasoning_effort == level

  def test_with_effort_rejects_a_level_outside_the_neutral_vocabulary(self):
    # 'minimal' is a valid reasoning_effort but not a neutral level — with_effort
    # speaks only the neutral vocabulary
    with pytest.raises(ValueError, match='unknown effort level'):
      Recipe().with_effort('minimal')

  def test_with_effort_returns_new_spec_preserving_other_knobs(self):
    spec = Recipe(model='gpt-5.4-mini', service_tier='priority')
    with_effort = spec.with_effort('high')
    assert with_effort.reasoning_effort == 'high'
    # original untouched (frozen) and a distinct instance
    assert spec.reasoning_effort is None
    assert with_effort is not spec
    # other fields preserved — composes with fast()'s service_tier
    assert with_effort.model == 'gpt-5.4-mini'
    assert with_effort.service_tier == 'priority'

  def test_frozen_rejects_mutation(self):
    spec = Recipe()
    # frozen dataclass raises FrozenInstanceError, a subclass of AttributeError
    with pytest.raises(AttributeError):
      spec.service_tier = 'priority'  # type: ignore[misc]

  def test_dump_round_trips_through_base_from_dict(self):
    spec = Recipe(
      model='gpt-5.4-mini',
      reasoning_effort='medium',
      service_tier='priority',
      compact_threshold=50_000,
    )
    restored = llm_module.Recipe.from_dict(spec.dump())
    # frozen dataclass auto-generates __eq__ — single assertion covers every field
    assert restored == spec

  def test_dump_round_trip_handles_missing_optional_keys(self):
    # legacy / hand-written payloads may omit fields that were absent on write
    restored = llm_module.Recipe.from_dict({'type': 'openai', 'model': 'gpt-5'})
    assert isinstance(restored, Recipe)
    assert restored == Recipe(model='gpt-5')

  def test_from_dict_works_without_pre_importing_provider_module(self):
    import subprocess
    import sys

    from bro.base.source_root import SOURCE_ROOT

    script = (
      'import sys; '
      "assert 'bro.openai.declaration' not in sys.modules; "
      'from bro.llm.llm import Recipe; '
      "spec = Recipe.from_dict({'type': 'openai', 'model': 'gpt-5'}); "
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
    import subprocess
    import sys

    from bro.base.source_root import SOURCE_ROOT

    script = (
      'import sys; '
      'import bro.openai.declaration; '
      "bro.openai.declaration.Recipe(reasoning_effort='medium'); "
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
    assert openai_llm.price('gpt-5.6-terra', usage, 'scale') is None

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

  def test_a_serialized_price_snapshot_can_be_reconstructed(self):
    current = openai_llm.PRICE_TABLE
    model = current.models['gpt-5.6-terra']
    snapshot = openai_llm.price_table_from_content(
      current.source,
      current.as_of.isoformat(),
      {'gpt-5.6-terra': model.content()},
    )
    usage = {'input_tokens': 1, 'output_tokens': 1}

    assert openai_llm.price('gpt-5.6-terra', usage, None, snapshot) == openai_llm.price(
      'gpt-5.6-terra', usage, None, current
    )

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


def _rates(
  value: str, *, cached_input: Optional[str] = None, cache_write: Optional[str] = None
) -> dict:
  return {'input': value, 'cached_input': cached_input, 'cache_write': cache_write, 'output': value}


def _tiers(**tiers: dict) -> dict:
  return {'long_context_threshold': None, 'service_tiers': tiers}


class TestPricingVocabulary:
  # the serialized shape retention manifests persist (`benchmark/bro/benchmark/retention.py`)
  TABLE = openai_llm.price_table_from_content(
    'https://example.com/pricing',
    date.today().isoformat(),
    {
      'model': _tiers(
        standard={'short': _rates('1', cached_input='1', cache_write='1'), 'long': None},
        flex={'short': _rates('2'), 'long': None},
        priority={'short': _rates('3'), 'long': None},
      ),
      'model-2024-05-13': _tiers(standard={'short': _rates('4'), 'long': None}),
      'tiered': {
        'long_context_threshold': 10,
        'service_tiers': {
          'standard': {'short': _rates('1'), 'long': _rates('2')},
          'priority': {'short': _rates('3'), 'long': None},
        },
      },
    },
  )

  @pytest.mark.parametrize(
    'service_tier,rate',
    [(None, 1), ('auto', 1), ('default', 1), ('flex', 2), ('fast', 3), ('priority', 3)],
  )
  def test_each_reported_service_tier_pays_its_tier_rates(self, service_tier, rate):
    usage = {'input_tokens': 1, 'output_tokens': 1}
    assert (
      openai_llm.price('model', usage, service_tier, self.TABLE) == Decimal(2 * rate) / 1_000_000
    )

  @pytest.mark.parametrize(
    'model,rate', [('model-2025-08-07', 1), ('model-2024-05-13', 4), ('model-latest', None)]
  )
  def test_a_dated_snapshot_without_a_row_pays_its_undated_models_rates(self, model, rate):
    cost = openai_llm.price(model, {'input_tokens': 1, 'output_tokens': 1}, None, self.TABLE)
    assert cost == (None if rate is None else Decimal(2 * rate) / 1_000_000)

  def test_a_class_the_model_does_not_bill_prices_while_its_usage_reports_none(self):
    usage = {'input_tokens': 1, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 1}
    assert openai_llm.price('model', usage, 'flex', self.TABLE) == Decimal(4) / 1_000_000

  @pytest.mark.parametrize('detail', ['cached_tokens', 'cache_write_tokens'])
  def test_tokens_in_a_class_the_model_does_not_bill_are_rejected(self, detail):
    usage = {'input_tokens': 2, 'input_tokens_details': {detail: 1}, 'output_tokens': 0}
    with pytest.raises(ValueError, match='bills no'):
      openai_llm.price('model', usage, 'flex', self.TABLE)

  def test_a_model_without_a_threshold_pays_its_rates_at_any_length(self):
    usage = {'input_tokens': 300_000, 'output_tokens': 0}
    assert openai_llm.price('model', usage, 'flex', self.TABLE) == Decimal('0.6')

  def test_a_long_call_on_a_tier_without_long_rates_is_not_treated_as_free(self):
    usage = {'input_tokens': 11, 'output_tokens': 0}
    assert openai_llm.price('tiered', usage, None, self.TABLE) == Decimal(22) / 1_000_000
    assert openai_llm.price('tiered', usage, 'priority', self.TABLE) is None

  def test_long_rates_require_a_threshold(self):
    with pytest.raises(ValueError, match='long_context_threshold'):
      openai_llm.price_table_from_content(
        'https://example.com',
        '2026-01-01',
        {'model': _tiers(standard={'short': _rates('1'), 'long': _rates('2')})},
      )

  def test_blank_classes_and_missing_long_rates_survive_a_serialized_snapshot(self):
    current = openai_llm.PRICE_TABLE
    model = current.models['gpt-5.4']
    snapshot = openai_llm.price_table_from_content(
      current.source, current.as_of.isoformat(), {'gpt-5.4': model.content()}
    )
    assert snapshot.models['gpt-5.4'].content() == model.content()
