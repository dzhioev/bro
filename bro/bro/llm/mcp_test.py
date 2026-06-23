from dataclasses import dataclass
from typing import Annotated, Optional

import pytest
from pydantic import Field

from llm import mcp as mcp_mod
from llm.mcp import (
  FunctionTool,
  InProcessMCPServer,
  ToolRegistry,
  UnknownToolError,
  describe,
  namespaced_tools,
  render_has_cred,
  wire_name,
)


class TestFunctionTool:
  def test_sync_function(self):
    def greet(name: Annotated[str, Field(description='person name')]) -> str:
      return f'hello {name}'

    describe(greet, 'say hello')

    tool = FunctionTool(greet)
    assert tool.name == 'greet'
    assert tool.description == 'say hello'
    assert 'name' in tool.parameters.get('properties', {})

  def test_custom_name_and_description(self):
    def greet(name: str) -> str:
      return f'hello {name}'

    describe(greet, 'original description')

    tool = FunctionTool(greet, name='say_hi', description='custom desc')
    assert tool.name == 'say_hi'
    assert tool.description == 'custom desc'

  def test_no_description_raises(self):
    def no_doc(x: str) -> str:
      return x

    with pytest.raises(ValueError, match='no description'):
      FunctionTool(no_doc)

  @pytest.mark.asyncio
  async def test_call_sync(self):
    def add(
      a: Annotated[int, Field(description='first')],
      b: Annotated[int, Field(description='second')],
    ) -> str:
      return str(a + b)

    describe(add, 'add two numbers')

    tool = FunctionTool(add)
    result = await tool.call({'a': 3, 'b': 4})
    assert result == '7'

  @pytest.mark.asyncio
  async def test_call_async(self):
    async def upper(text: Annotated[str, Field(description='input text')]) -> str:
      return text.upper()

    describe(upper, 'uppercase text')

    tool = FunctionTool(upper)
    result = await tool.call({'text': 'hello'})
    assert result == 'HELLO'

  def test_optional_parameter(self):
    def search(
      query: Annotated[str, Field(description='search query')],
      limit: Annotated[Optional[int], Field(description='max results')] = None,
    ) -> str:
      return f'{query}:{limit}'

    describe(search, 'search for items')

    tool = FunctionTool(search)
    props = tool.parameters.get('properties', {})
    assert 'query' in props
    assert 'limit' in props

  @pytest.mark.asyncio
  async def test_call_with_optional_default(self):
    def greet(
      name: Annotated[str, Field(description='name')],
      greeting: Annotated[str, Field(description='greeting')] = 'hello',
    ) -> str:
      return f'{greeting} {name}'

    describe(greet, 'greet someone')

    tool = FunctionTool(greet)
    result = await tool.call({'name': 'world'})
    assert result == 'hello world'

    result = await tool.call({'name': 'world', 'greeting': 'hi'})
    assert result == 'hi world'

  def test_str_return_has_no_output_schema(self):
    def echo(text: Annotated[str, Field(description='text')]) -> str:
      return text

    describe(echo, 'echo')

    tool = FunctionTool(echo)
    assert tool.output_schema is None

  @pytest.mark.asyncio
  async def test_dataclass_return_produces_structured_output(self):
    @dataclass
    class Point:
      x: int
      y: int
      label: Optional[str]

    def make_point(
      x: Annotated[int, Field(description='x')],
      y: Annotated[int, Field(description='y')],
    ) -> Point:
      return Point(x=x, y=y, label=None)

    describe(make_point, 'make a point')

    tool = FunctionTool(make_point)
    schema = tool.output_schema
    assert schema is not None
    assert set(schema['properties'].keys()) == {'x', 'y', 'label'}

    result = await tool.call({'x': 1, 'y': 2})
    assert result == {'x': 1, 'y': 2, 'label': None}


class TestInProcessMCPServer:
  @pytest.mark.asyncio
  async def test_list_tools(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    def tool_b(y: Annotated[int, Field(description='number')]) -> str:
      return str(y)

    describe(tool_b, 'tool b')

    server = InProcessMCPServer('test', [FunctionTool(tool_a), FunctionTool(tool_b)])
    tools = await server.list_tools()
    assert len(tools) == 2
    # the server's own list_tools returns the local (in-namespace) names; the
    # namespacing happens at the assembling layer (ToolRegistry / _Aggregate).
    assert tools[0].name == 'tool_a'
    assert tools[1].name == 'tool_b'

  @pytest.mark.asyncio
  async def test_empty_server(self):
    server = InProcessMCPServer('test', [])
    tools = await server.list_tools()
    assert tools == []

  @pytest.mark.asyncio
  async def test_rejects_double_underscore_in_namespace(self):
    with pytest.raises(ValueError, match='double underscore'):
      InProcessMCPServer('bad__ns', [])

  @pytest.mark.asyncio
  async def test_rejects_empty_namespace(self):
    with pytest.raises(ValueError, match='non-empty'):
      InProcessMCPServer('', [])

  @pytest.mark.asyncio
  async def test_rejects_double_underscore_in_tool_name(self):
    def bad(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(bad, 'bad tool')

    with pytest.raises(ValueError, match='double underscore'):
      InProcessMCPServer('test', [FunctionTool(bad, name='bad__tool')])

  @pytest.mark.asyncio
  async def test_list_tools_returns_copy(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    server = InProcessMCPServer('test', [FunctionTool(tool_a)])
    tools1 = await server.list_tools()
    tools2 = await server.list_tools()
    assert tools1 is not tools2
    assert tools1[0] is tools2[0]


class TestToolRegistry:
  @pytest.mark.asyncio
  async def test_resolve_from_single_server(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    registry = ToolRegistry([InProcessMCPServer('a', [FunctionTool(tool_a)])])
    tools = await registry.resolve()
    assert len(tools) == 1
    # the registry advertises namespaced wire names to the LLM.
    assert tools[0].name == 'a__tool_a'

  @pytest.mark.asyncio
  async def test_resolve_from_multiple_servers(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    def tool_b(y: Annotated[int, Field(description='number')]) -> str:
      return str(y)

    describe(tool_b, 'tool b')

    server_a = InProcessMCPServer('a', [FunctionTool(tool_a)])
    server_b = InProcessMCPServer('b', [FunctionTool(tool_b)])
    registry = ToolRegistry([server_a, server_b])
    tools = await registry.resolve()
    assert len(tools) == 2
    names = {t.name for t in tools}
    assert names == {'a__tool_a', 'b__tool_b'}

  @pytest.mark.asyncio
  async def test_same_local_name_distinct_across_namespaces(self):
    # the collision case namespaces exist to solve: two sources both exposing a
    # `search` tool stay distinct because the wire name carries the namespace.
    def search(query: Annotated[str, Field(description='q')]) -> str:
      return query

    describe(search, 'search')

    server_a = InProcessMCPServer('wikipedia-source', [FunctionTool(search)])
    server_b = InProcessMCPServer('tmdb-source', [FunctionTool(search)])
    registry = ToolRegistry([server_a, server_b])
    names = {t.name for t in await registry.resolve()}
    assert names == {'wikipedia-source__search', 'tmdb-source__search'}

  @pytest.mark.asyncio
  async def test_resolve_caches(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    registry = ToolRegistry([InProcessMCPServer('a', [FunctionTool(tool_a)])])
    tools1 = await registry.resolve()
    tools2 = await registry.resolve()
    assert tools1[0] is tools2[0]

  @pytest.mark.asyncio
  async def test_duplicate_name_raises(self):
    def dupe(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(dupe, 'duplicate')

    server_a = InProcessMCPServer('a', [FunctionTool(dupe)])
    server_b = InProcessMCPServer('a', [FunctionTool(dupe)])
    registry = ToolRegistry([server_a, server_b])
    with pytest.raises(ValueError, match="duplicate tool wire name.*namespace 'a'"):
      await registry.resolve()

  @pytest.mark.asyncio
  async def test_call_by_name(self):
    def reverse(text: Annotated[str, Field(description='text')]) -> str:
      return text[::-1]

    describe(reverse, 'reverse text')

    registry = ToolRegistry([InProcessMCPServer('a', [FunctionTool(reverse)])])
    result = await registry.call('a__reverse', {'text': 'hello'})
    assert result == 'olleh'

  @pytest.mark.asyncio
  async def test_call_unknown_raises(self):
    registry = ToolRegistry([InProcessMCPServer('test', [])])
    with pytest.raises(UnknownToolError, match="unknown or disallowed tool: 'nonexistent'") as exc:
      await registry.call('nonexistent', {})
    assert exc.value.name == 'nonexistent'

  @pytest.mark.asyncio
  async def test_empty_registry(self):
    registry = ToolRegistry([])
    tools = await registry.resolve()
    assert tools == []


class TestRenderHasCred:
  @staticmethod
  def _avail(*present: str):
    return lambda name: name in set(present)

  def test_present_branch_when_available(self):
    tmpl = 'base{{#has_cred openai}} yes{{else}} no{{/has_cred}}'
    assert render_has_cred(tmpl, self._avail('openai'), ['openai']) == 'base yes'

  def test_else_branch_when_absent(self):
    tmpl = 'base{{#has_cred openai}} yes{{else}} no{{/has_cred}}'
    assert render_has_cred(tmpl, self._avail(), ['openai']) == 'base no'

  def test_no_else_yields_empty_when_absent(self):
    tmpl = 'base{{#has_cred openai}} yes{{/has_cred}}'
    assert render_has_cred(tmpl, self._avail(), ['openai']) == 'base'

  def test_inverted_renders_only_when_absent(self):
    tmpl = 'x{{^has_cred openai}} (no key){{/has_cred}}'
    assert render_has_cred(tmpl, self._avail(), ['openai']) == 'x (no key)'
    assert render_has_cred(tmpl, self._avail('openai'), ['openai']) == 'x'

  def test_no_marker_returned_unchanged_without_reading_availability(self):
    def boom(name: str) -> bool:
      raise AssertionError('availability must not be consulted with no block')

    assert render_has_cred('plain text', boom, []) == 'plain text'

  def test_undeclared_name_raises(self):
    with pytest.raises(ValueError, match='undeclared secret'):
      render_has_cred('{{#has_cred typo}}x{{/has_cred}}', self._avail(), ['openai'])

  @pytest.mark.asyncio
  async def test_namespaced_tool_renders_description_against_availability(self, monkeypatch):
    def fetch(id: Annotated[str, Field(description='id')]) -> str:
      return id

    describe(fetch, 'fetch a record{{#has_cred openai}}; summarized{{else}}; raw only{{/has_cred}}')

    class Srv(InProcessMCPServer):
      optional_secrets = ('openai',)

      def __init__(self):
        super().__init__('src', [FunctionTool(fetch)])

    monkeypatch.setattr(mcp_mod.credentials, 'available', lambda name: False)
    tools = await namespaced_tools(Srv())
    assert tools[0].name == 'src__fetch'
    assert tools[0].description == 'fetch a record; raw only'


class TestWireName:
  def test_joins_with_double_underscore(self):
    assert wire_name('flow', 'get_task_info') == 'flow__get_task_info'

  def test_local_hyphen_and_single_underscore_preserved(self):
    assert wire_name('wikipedia-source', 'get_time') == 'wikipedia-source__get_time'
