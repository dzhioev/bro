import json
import pytest

from bro.bro import BaseBro, BroRaised, ScatterTool, Tool
from bro.datasources.base import DataSource, Hit
from llm.llm import LLM
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, describe
from llm.tracer import NullTracer, Tracer


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: list[MCPServer] | None = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict]) -> str:
    self.send_calls.append(messages)
    return self.response


class EchoBro(BaseBro):
  name = 'echo'
  description = 'echoes input'

  def __init__(self, response: str = 'echoed'):
    super().__init__(system_prompt='you echo')
    self._response = response

  def _make_tracer(self) -> Tracer:
    return NullTracer()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return MockLLM(response=self._response)


class TestBroRun:
  @pytest.mark.asyncio
  async def test_run_returns_response(self):
    bro = EchoBro(response='hello back')
    result = await bro.run('hello')
    assert result == 'hello back'

  @pytest.mark.asyncio
  async def test_run_wires_tracer_through_to_llm(self):
    captured: list[Tracer] = []

    class CapturingTracer(NullTracer):
      pass

    class WireBro(BaseBro):
      name = 'wire'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _make_tracer(self) -> Tracer:
        return CapturingTracer()

      def _create_llm(self, *, interactive: bool):
        captured.append(self._tracer)
        return MockLLM()

    await WireBro().run('hi')
    assert len(captured) == 1
    assert isinstance(captured[0], CapturingTracer)

  @pytest.mark.asyncio
  async def test_run_explicit_tracer_overrides_make_tracer(self):
    captured: list[Tracer] = []

    class MadeTracer(NullTracer):
      pass

    class ExplicitTracer(NullTracer):
      pass

    class OverrideBro(BaseBro):
      name = 'override'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _make_tracer(self) -> Tracer:
        return MadeTracer()

      def _create_llm(self, *, interactive: bool):
        captured.append(self._tracer)
        return MockLLM()

    explicit = ExplicitTracer()
    await OverrideBro().run('hi', tracer=explicit)
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_run_passes_system_and_user_messages(self):
    llm = MockLLM()

    class CaptureBro(BaseBro):
      name = 'capture'
      description = 'captures messages'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self, *, interactive: bool):
        return llm

    bro = CaptureBro()
    await bro.run('test input')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert 'be helpful' in messages[0]['content']
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

    class TrackBro(BaseBro):
      name = 'track'
      description = 'tracks'

      def __init__(self):
        super().__init__(system_prompt='track')

      def _create_llm(self, *, interactive: bool):
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

    class CaptureBro(BaseBro):
      name = 'capture'
      description = 'captures'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self, *, interactive: bool):
        return llm

    bro = CaptureBro()
    await bro.send('hi')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert 'be helpful' in messages[0]['content']
    assert messages[1] == {'role': 'user', 'content': 'hi'}

  @pytest.mark.asyncio
  async def test_send_wires_explicit_tracer(self):
    captured: list[Tracer] = []

    class TracerTracer(NullTracer):
      pass

    class WireBro(BaseBro):
      name = 'wire-send'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, interactive: bool):
        captured.append(self._tracer)
        return MockLLM()

    explicit = TracerTracer()
    bro = WireBro()
    await bro.send('hi', tracer=explicit)
    await bro.send('again')
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_send_subsequent_calls_only_user(self):
    llm = MockLLM()

    class CaptureBro(BaseBro):
      name = 'capture'
      description = 'captures'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self, *, interactive: bool):
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

    class TrackBro(BaseBro):
      name = 'track'
      description = 'tracks'

      def __init__(self):
        super().__init__(system_prompt='track')

      def _create_llm(self, *, interactive: bool):
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

    class CountBro(BaseBro):
      name = 'counter'
      description = 'counts'

      def __init__(self):
        super().__init__(system_prompt='count')

      def _create_llm(self, *, interactive: bool):
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
    class SourceBro(BaseBro):
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
    class SourceBro(BaseBro):
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
  async def test_instance_entry_exposes_its_tools(self):
    server = _make_server('a', 'b', 'c')

    class InstanceBro(BaseBro):
      name = 'instance'
      description = 'd'
      mcp_servers = [server]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = InstanceBro()
    assert bro._mcp_servers == [server]
    tools = await bro._mcp_servers[0].list_tools()
    assert {t.name for t in tools} == {'a', 'b', 'c'}

  @pytest.mark.asyncio
  async def test_factory_entry_called_once_per_instance(self):
    calls = 0

    def factory():
      nonlocal calls
      calls += 1
      return _make_server('a')

    class CountBro(BaseBro):
      name = 'count'
      description = 'd'
      mcp_servers = [factory]

      def __init__(self):
        super().__init__(system_prompt='')

    CountBro()
    CountBro()
    assert calls == 2

  @pytest.mark.asyncio
  async def test_extend_mcp_servers_appends(self):
    class EmptyBro(BaseBro):
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


async def _collect_tool_names(servers):
  names: set[str] = set()
  for server in servers:
    for tool in await server.list_tools():
      names.add(tool.name)
  return names


async def _find_raise_tool(bro: BaseBro):
  for tool in await bro._service_server.list_tools():
    if tool.name == 'raise':
      return tool
  raise AssertionError('raise tool not found on bro service server')


class TestRaise:
  @pytest.mark.asyncio
  async def test_raise_tool_included_in_non_interactive_mode(self):
    bro = EchoBro()
    names = await _collect_tool_names(bro._mcp_servers_for(interactive=False))
    assert 'raise' in names

  @pytest.mark.asyncio
  async def test_raise_tool_excluded_in_interactive_mode(self):
    bro = EchoBro()
    names = await _collect_tool_names(bro._mcp_servers_for(interactive=True))
    assert 'raise' not in names

  @pytest.mark.asyncio
  async def test_raise_tool_raises_bro_raised(self):
    bro = EchoBro()
    tool = await _find_raise_tool(bro)
    with pytest.raises(BroRaised) as exc:
      await tool.call({'reason': 'missing api key'})
    assert exc.value.reason == 'missing api key'

  def test_non_interactive_system_prompt_includes_note(self):
    bro = EchoBro()
    prompt = bro._system_prompt_for(interactive=False)
    assert 'non-interactive' in prompt
    assert '`raise`' in prompt
    assert 'unclear' in prompt
    assert bro.system_prompt in prompt

  @pytest.mark.asyncio
  async def test_raise_tool_description_covers_unclear_input(self):
    bro = EchoBro()
    tool = await _find_raise_tool(bro)
    assert 'unclear' in tool.description

  def test_interactive_system_prompt_includes_note(self):
    bro = EchoBro()
    prompt = bro._system_prompt_for(interactive=True)
    assert 'non-interactive' not in prompt
    assert 'interactive mode' in prompt
    assert 'clarifying question' in prompt
    assert bro.system_prompt in prompt

  @pytest.mark.asyncio
  async def test_run_creates_llm_in_non_interactive_mode(self):
    captured: list[bool] = []

    class CaptureBro(BaseBro):
      name = 'capture-mode'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, interactive: bool):
        captured.append(interactive)
        return MockLLM()

    await CaptureBro().run('input')
    assert captured == [False]

  @pytest.mark.asyncio
  async def test_send_creates_llm_in_interactive_mode(self):
    captured: list[bool] = []

    class CaptureBro(BaseBro):
      name = 'capture-mode'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, interactive: bool):
        captured.append(interactive)
        return MockLLM()

    await CaptureBro().send('input')
    assert captured == [True]

  @pytest.mark.asyncio
  async def test_sub_bro_raise_propagates_through_parent_tool(self):
    class RaiseBro(BaseBro):
      name = 'raiser'
      description = 'always raises'

      def __init__(self):
        super().__init__(system_prompt='')

      async def run(self, input: str, tracer: Tracer | None = None) -> str:
        raise BroRaised('inner failure')

    tool = Tool(RaiseBro())
    with pytest.raises(BroRaised) as exc:
      await tool.call({'input': 'anything'})
    assert exc.value.reason == 'inner failure'
