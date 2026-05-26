from types import SimpleNamespace
from typing import cast

import pytest
from openai.types.responses import Response

from llm.llms.chat_gpt import ChatGPT
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
