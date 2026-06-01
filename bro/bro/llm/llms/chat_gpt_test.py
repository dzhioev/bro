from types import SimpleNamespace
from typing import cast

import pytest
from openai.types.responses import Response

import llm.llm
from llm.llms.chat_gpt import ChatGPT, LLMSpec
from llm.mcp import InProcessMCPServer, Tool, ToolControlSignal, ToolRegistry


class _StaticTool(Tool):
  def __init__(self, name: str, raise_with: Exception | None = None):
    self._name = name
    self._raise_with = raise_with

  @property
  def name(self) -> str:
    return self._name

  @property
  def description(self) -> str:
    return 'test tool'

  @property
  def parameters(self) -> dict:
    return {'type': 'object', 'properties': {}}

  async def call(self, arguments: dict):
    if self._raise_with is not None:
      raise self._raise_with
    return 'ok'


def _function_call_response(name: str) -> Response:
  # SimpleNamespace duck-types the few fields _execute_tool_calls reads
  # (output[*].type / .name / .arguments / .call_id); cast keeps pyright quiet.
  return cast(
    Response,
    SimpleNamespace(
      output=[
        SimpleNamespace(
          type='function_call',
          name=name,
          arguments='{}',
          call_id='call_1',
        )
      ]
    ),
  )


def _make_chat_gpt(tools: list[Tool]) -> ChatGPT:
  gpt = ChatGPT(api_key='dummy')
  gpt.tools = ToolRegistry([InProcessMCPServer(tools)])
  return gpt


@pytest.mark.asyncio
async def test_tool_exception_becomes_function_call_output():
  tool = _StaticTool('boom', raise_with=RuntimeError('upstream down'))
  gpt = _make_chat_gpt([tool])

  results = await gpt._execute_tool_calls(_function_call_response('boom'))

  assert len(results) == 1
  result = cast(dict, results[0])
  assert result['type'] == 'function_call_output'
  assert result['call_id'] == 'call_1'
  assert "'boom' failed" in result['output']
  assert 'RuntimeError' in result['output']
  assert 'upstream down' in result['output']


@pytest.mark.asyncio
async def test_tool_control_signal_propagates_past_loop():
  class _Abort(ToolControlSignal):
    pass

  tool = _StaticTool('halt', raise_with=_Abort('stop the run'))
  gpt = _make_chat_gpt([tool])

  with pytest.raises(_Abort, match='stop the run'):
    await gpt._execute_tool_calls(_function_call_response('halt'))


@pytest.mark.asyncio
async def test_successful_tool_call_returns_output_unchanged():
  gpt = _make_chat_gpt([_StaticTool('ping')])

  results = await gpt._execute_tool_calls(_function_call_response('ping'))

  assert len(results) == 1
  assert cast(dict, results[0])['output'] == 'ok'


class TestLLMSpec:
  def test_default_spec_has_no_optional_knobs(self):
    spec = LLMSpec()
    assert spec.model == 'gpt-5'
    assert spec.reasoning_effort is None
    assert spec.service_tier is None

  def test_invalid_service_tier_rejected(self):
    with pytest.raises(ValueError, match='invalid service_tier'):
      LLMSpec(service_tier='nope')  # type: ignore[arg-type]

  def test_invalid_reasoning_effort_rejected(self):
    with pytest.raises(ValueError, match='invalid reasoning_effort'):
      LLMSpec(reasoning_effort='ludicrous')  # type: ignore[arg-type]

  def test_fast_returns_new_spec_with_priority_tier(self):
    spec = LLMSpec(model='gpt-5.4-mini', reasoning_effort='medium')
    fast = spec.fast()
    assert fast.service_tier == 'priority'
    # original untouched (frozen) and a distinct instance
    assert spec.service_tier is None
    assert fast is not spec
    # other fields preserved
    assert fast.model == 'gpt-5.4-mini'
    assert fast.reasoning_effort == 'medium'

  def test_frozen_rejects_mutation(self):
    spec = LLMSpec()
    with pytest.raises(Exception):  # FrozenInstanceError is a subclass of AttributeError
      spec.service_tier = 'priority'  # type: ignore[misc]

  def test_dump_round_trips_through_base_from_dict(self):
    spec = LLMSpec(model='gpt-5.4-mini', reasoning_effort='medium', service_tier='priority')
    restored = llm.llm.LLMSpec.from_dict(spec.dump())
    # frozen dataclass auto-generates __eq__ — single assertion covers every field
    assert restored == spec

  def test_dump_round_trip_handles_missing_optional_keys(self):
    # legacy / hand-written payloads may omit fields that were absent on write
    restored = llm.llm.LLMSpec.from_dict({'type': 'chat_gpt', 'model': 'gpt-5'})
    assert isinstance(restored, LLMSpec)
    assert restored == LLMSpec(model='gpt-5')

  def test_from_dict_works_without_pre_importing_provider_module(self):
    # Run in a fresh interpreter so `llm.llms.chat_gpt` is genuinely absent at
    # call time — simulates a process (e.g. an ad-hoc decisions_log reader)
    # that imports only `llm.llm` and expects `from_dict` to still dispatch.
    # In-process monkeypatching would leave the dataclass class registered on
    # `LLMSpec.__subclasses__` even after sys.modules restoration.
    import subprocess
    import sys

    from base.project_root import PROJECT_ROOT

    script = (
      'import sys; '
      "assert 'llm.llms.chat_gpt' not in sys.modules; "
      'from llm.llm import LLMSpec; '
      "spec = LLMSpec.from_dict({'type': 'chat_gpt', 'model': 'gpt-5'}); "
      "assert spec.model == 'gpt-5'; "
      "assert spec.TYPE == 'chat_gpt'"
    )
    result = subprocess.run(
      [sys.executable, '-c', script], capture_output=True, text=True, cwd=PROJECT_ROOT
    )
    assert result.returncode == 0, f'stderr: {result.stderr}'
