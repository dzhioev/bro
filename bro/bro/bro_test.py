import json
import pytest

from bro.bro import Bro, ScatterTool, Tool
from llm.llm import LLM
from llm.mcp import MCPServer


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: list[MCPServer] | None = None):
    super().__init__(mcp_servers)
    self.response = response
    self.told_messages: list[dict] = []

  async def tell(self, messages: list[dict]) -> None:
    self.told_messages = messages

  async def ask(self) -> str:
    return self.response


class EchoBro(Bro):
  name = 'echo'
  description = 'echoes input'
  system_prompt = 'you echo'

  def __init__(self, response: str = 'echoed'):
    self._response = response

  def _create_llm(self) -> LLM:
    return MockLLM(response=self._response)


class TestBroRun:
  @pytest.mark.asyncio
  async def test_run_returns_response(self):
    bro = EchoBro(response='hello back')
    result = await bro.run('hello')
    assert result == 'hello back'

  @pytest.mark.asyncio
  async def test_run_passes_system_and_user_messages(self):
    llm = MockLLM()

    class CaptureBro(Bro):
      name = 'capture'
      description = 'captures messages'
      system_prompt = 'be helpful'

      def _create_llm(self):
        return llm

    bro = CaptureBro()
    await bro.run('test input')
    assert len(llm.told_messages) == 2
    assert llm.told_messages[0]['role'] == 'system'
    assert llm.told_messages[0]['content'].endswith('be helpful')
    assert llm.told_messages[1] == {'role': 'user', 'content': 'test input'}


class TestBroMap:
  @pytest.mark.asyncio
  async def test_map_returns_all_results(self):
    call_count = 0

    class CountBro(Bro):
      name = 'counter'
      description = 'counts'
      system_prompt = 'count'

      def _create_llm(self):
        nonlocal call_count
        call_count += 1
        return MockLLM(response=f'result-{call_count}')

    bro = CountBro()
    results = await bro.map(['a', 'b', 'c'])
    assert len(results) == 3
    assert all(r.startswith('result-') for r in results)

  @pytest.mark.asyncio
  async def test_map_empty_inputs(self):
    bro = EchoBro()
    results = await bro.map([])
    assert results == []


class TestTool:
  @pytest.mark.asyncio
  async def test_tool_name_and_description(self):
    bro = EchoBro()
    tool = Tool(bro)
    assert tool.name == 'echo'
    assert tool.description == 'echoes input'

  @pytest.mark.asyncio
  async def test_tool_parameters_schema(self):
    bro = EchoBro()
    tool = Tool(bro)
    params = tool.parameters
    assert params['type'] == 'object'
    assert 'input' in params['properties']
    assert params['required'] == ['input']

  @pytest.mark.asyncio
  async def test_tool_call(self):
    bro = EchoBro(response='tool result')
    tool = Tool(bro)
    result = await tool.call({'input': 'hi'})
    assert result == 'tool result'


class TestScatterTool:
  @pytest.mark.asyncio
  async def test_scatter_tool_name(self):
    bro = EchoBro()
    tool = ScatterTool(bro)
    assert tool.name == 'echo-scatter'

  @pytest.mark.asyncio
  async def test_scatter_tool_description(self):
    bro = EchoBro()
    tool = ScatterTool(bro)
    assert 'parallel' in tool.description

  @pytest.mark.asyncio
  async def test_scatter_tool_parameters_schema(self):
    bro = EchoBro()
    tool = ScatterTool(bro)
    params = tool.parameters
    assert params['type'] == 'object'
    assert params['properties']['inputs']['type'] == 'array'
    assert params['required'] == ['inputs']

  @pytest.mark.asyncio
  async def test_scatter_tool_call(self):
    bro = EchoBro(response='done')
    tool = ScatterTool(bro)
    result = await tool.call({'inputs': ['a', 'b']})
    parsed = json.loads(result)
    assert parsed == ['done', 'done']
