import json
import pytest

from bro.bro import Bro, McpServerSpec, ScatterTool, Tool
from bro.datasources.base import DataSource, Hit
from llm.llm import LLM
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, describe


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: list[MCPServer] | None = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict]) -> str:
    self.send_calls.append(messages)
    return self.response


class EchoBro(Bro):
  name = 'echo'
  description = 'echoes input'

  def __init__(self, response: str = 'echoed'):
    super().__init__(system_prompt='you echo')
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

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self):
        return llm

    bro = CaptureBro()
    await bro.run('test input')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert messages[0]['content'].endswith('be helpful')
    assert messages[1] == {'role': 'user', 'content': 'test input'}


class TestBroSend:
  @pytest.mark.asyncio
  async def test_send_returns_response(self):
    bro = EchoBro(response='hello')
    result = await bro.send('hi')
    assert result == 'hello'

  @pytest.mark.asyncio
  async def test_send_reuses_llm(self):
    llm_instances = []

    class TrackBro(Bro):
      name = 'track'
      description = 'tracks'

      def __init__(self):
        super().__init__(system_prompt='track')

      def _create_llm(self):
        llm = MockLLM()
        llm_instances.append(llm)
        return llm

    bro = TrackBro()
    await bro.send('a')
    await bro.send('b')
    assert len(llm_instances) == 1

  @pytest.mark.asyncio
  async def test_send_first_call_includes_system_prompt(self):
    llm = MockLLM()

    class CaptureBro(Bro):
      name = 'capture'
      description = 'captures'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self):
        return llm

    bro = CaptureBro()
    await bro.send('hi')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert messages[0]['content'].endswith('be helpful')
    assert messages[1] == {'role': 'user', 'content': 'hi'}

  @pytest.mark.asyncio
  async def test_send_subsequent_calls_only_user(self):
    llm = MockLLM()

    class CaptureBro(Bro):
      name = 'capture'
      description = 'captures'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self):
        return llm

    bro = CaptureBro()
    await bro.send('first')
    await bro.send('second')
    assert len(llm.send_calls) == 2
    messages = llm.send_calls[1]
    assert len(messages) == 1
    assert messages[0] == {'role': 'user', 'content': 'second'}

  @pytest.mark.asyncio
  async def test_run_does_not_affect_send_llm(self):
    llm_instances = []

    class TrackBro(Bro):
      name = 'track'
      description = 'tracks'

      def __init__(self):
        super().__init__(system_prompt='track')

      def _create_llm(self):
        llm = MockLLM()
        llm_instances.append(llm)
        return llm

    bro = TrackBro()
    await bro.run('one-shot')
    await bro.send('first')
    await bro.send('second')
    assert len(llm_instances) == 2


class TestBroMap:
  @pytest.mark.asyncio
  async def test_map_returns_all_results(self):
    call_count = 0

    class CountBro(Bro):
      name = 'counter'
      description = 'counts'

      def __init__(self):
        super().__init__(system_prompt='count')

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


class _StubSource(DataSource):
  name = 'stub'
  summary = 'a stub data source for tests'

  def __init__(self):
    self.fetch_calls: list[tuple[str, str | None]] = []

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return [Hit(id='stub-1', title=f'hit for {query}', snippet='stub snippet')]

  async def fetch(self, id: str, query: str | None = None) -> str:
    self.fetch_calls.append((id, query))
    return f'content for {id}'


class TestBroDataSources:
  @pytest.mark.asyncio
  async def test_data_source_mcp_server_mounted(self):
    class SourceBro(Bro):
      name = 'with-source'
      description = 'has a data source'
      data_sources = [_StubSource()]

      def __init__(self):
        super().__init__(system_prompt='hi')

    bro = SourceBro()
    servers = bro._mcp_servers
    assert len(servers) == 1
    tools = await servers[0].list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {'stub-search', 'stub-fetch'}

  def test_data_source_summary_in_system_prompt(self):
    class SourceBro(Bro):
      name = 'summary-bro'
      description = 'd'
      data_sources = [_StubSource()]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = SourceBro()
    assert '## Data sources' in bro.system_prompt
    assert '**stub**' in bro.system_prompt
    assert 'a stub data source for tests' in bro.system_prompt

  @pytest.mark.asyncio
  async def test_data_source_search_and_fetch_calls(self):
    source = _StubSource()
    server = source.as_mcp_server()
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    search_result = await by_name['stub-search'].call({'query': 'foo'})
    assert isinstance(search_result, str)
    parsed = json.loads(search_result)
    assert parsed[0]['id'] == 'stub-1'
    fetch_result = await by_name['stub-fetch'].call({'id': 'x', 'query': 'why'})
    assert fetch_result == 'content for x'
    assert source.fetch_calls == [('x', 'why')]


def _make_server(*tool_names: str) -> InProcessMCPServer:
  tools = []
  for name in tool_names:

    def fn() -> str:
      return 'ok'

    fn.__name__ = name
    describe(fn, f'{name} tool')
    tools.append(FunctionTool(fn))
  return InProcessMCPServer(tools)


class TestBroMcpServers:
  @pytest.mark.asyncio
  async def test_bare_factory_exposes_all_tools(self):
    class FactoryBro(Bro):
      name = 'factory'
      description = 'd'
      mcp_servers = [lambda: _make_server('a', 'b', 'c')]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = FactoryBro()
    assert len(bro._mcp_servers) == 1
    tools = await bro._mcp_servers[0].list_tools()
    assert {t.name for t in tools} == {'a', 'b', 'c'}

  @pytest.mark.asyncio
  async def test_factory_called_once_per_instance(self):
    calls = 0

    def factory():
      nonlocal calls
      calls += 1
      return _make_server('a')

    class CountBro(Bro):
      name = 'count'
      description = 'd'
      mcp_servers = [factory]

      def __init__(self):
        super().__init__(system_prompt='')

    CountBro()
    CountBro()
    assert calls == 2

  @pytest.mark.asyncio
  async def test_spec_with_allowlist_filters_tools(self):
    class AllowBro(Bro):
      name = 'allow'
      description = 'd'
      mcp_servers = [McpServerSpec(lambda: _make_server('a', 'b', 'c'), allowed_tools=['a', 'c'])]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = AllowBro()
    tools = await bro._mcp_servers[0].list_tools()
    assert [t.name for t in tools] == ['a', 'c']

  @pytest.mark.asyncio
  async def test_allowlist_with_unknown_tool_raises(self):
    class BadBro(Bro):
      name = 'bad'
      description = 'd'
      mcp_servers = [McpServerSpec(lambda: _make_server('a'), allowed_tools=['ghost'])]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = BadBro()
    with pytest.raises(ValueError, match='unknown tools in allowlist'):
      await bro._mcp_servers[0].list_tools()

  @pytest.mark.asyncio
  async def test_extend_mcp_servers_appends(self):
    class EmptyBro(Bro):
      name = 'empty'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    bro = EmptyBro()
    assert bro._mcp_servers == []
    extra = _make_server('x')
    bro.extend_mcp_servers([extra])
    assert bro._mcp_servers == [extra]


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
