import subprocess
import sys
from typing import get_args

import pytest

import bro.llm.llm as llm_module
import bro.llm.llms.openai as openai_llm
from bro.base.source_root import SOURCE_ROOT
from bro.llm.llms.openai import LLMSpec


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
