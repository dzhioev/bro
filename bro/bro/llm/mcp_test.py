from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Optional

import pytest
from pydantic import Field, ValidationError

from llm import mcp as mcp_mod
from llm.mcp import (
  FunctionTool,
  InProcessMCPServer,
  ToolRegistry,
  UnknownToolError,
  describe,
  namespaced_tools,
  render_return_shape,
  render_text,
  validated_callable,
  wire_name,
)


class _Color(Enum):
  RED = 'red'
  BLUE = 'blue'


@dataclass
class _Item:
  id: str
  tags: list[str]
  color: _Color
  note: Optional[str]


class TestReturnShape:
  def _schema(self, function) -> dict:
    describe(function, 'd')
    schema = FunctionTool(function).output_schema
    assert schema is not None
    return schema

  def test_object_with_enum_list_and_optional(self):
    def make() -> _Item:
      raise NotImplementedError

    assert render_return_shape(self._schema(make)) == (
      '{\n  id: str,\n  tags: str[],\n  color: "red"|"blue",\n  note: str|null\n}'
    )

  def test_list_return_unwraps_and_names_element(self):
    def make() -> list[_Item]:
      raise NotImplementedError

    assert render_return_shape(self._schema(make)).startswith('_Item{')
    assert render_return_shape(self._schema(make)).endswith('}[]')

  def test_optional_return_unwraps_to_nullable(self):
    def make() -> Optional[_Item]:
      raise NotImplementedError

    assert render_return_shape(self._schema(make)).endswith('}|null')

  def test_str_return_omits_shape_from_description(self):
    def echo(x: Annotated[str, Field(description='x')]) -> str:
      return x

    describe(echo, 'echo')
    assert FunctionTool(echo).description == 'echo'

  def test_structured_return_appends_shape_to_description(self):
    def make() -> _Item:
      raise NotImplementedError

    describe(make, 'make an item')
    description = FunctionTool(make).description
    assert description.startswith('make an item\n\nReturns: {')


@dataclass
class _Pair:
  x: int
  label: str


class TestOutputValidation:
  def _tool(self, function) -> FunctionTool:
    describe(function, 'd')
    return FunctionTool(function)

  def test_validate_output_accepts_conforming(self):
    def make() -> _Pair:
      return _Pair(x=1, label='a')

    self._tool(make).validate_output(_Pair(x=1, label='a'))

  def test_validate_output_raises_on_wrong_type(self):
    def make() -> _Pair:
      return _Pair(x=1, label='a')

    with pytest.raises(ValidationError):
      self._tool(make).validate_output({'x': 'not-an-int', 'label': 'a'})

  def test_validate_output_raises_on_missing_field(self):
    def make() -> _Pair:
      return _Pair(x=1, label='a')

    with pytest.raises(ValidationError):
      self._tool(make).validate_output({'x': 1})

  def test_validate_output_raises_on_wrong_list_shape(self):
    def make() -> list[_Pair]:
      return [_Pair(x=1, label='a')]

    with pytest.raises(ValidationError):
      self._tool(make).validate_output('not-a-list')

  def test_validate_output_str_tool_rejects_non_str(self):
    def echo(x: Annotated[str, Field(description='x')]) -> str:
      return x

    tool = self._tool(echo)
    tool.validate_output('fine')
    with pytest.raises(AssertionError):
      tool.validate_output(123)

  @pytest.mark.asyncio
  async def test_validated_callable_returns_result_unchanged(self):
    def make(n: Annotated[int, Field(description='n')]) -> _Pair:
      return _Pair(x=n, label='a')

    wrapped = validated_callable(self._tool(make))
    assert await wrapped(n=5) == _Pair(x=5, label='a')

  @pytest.mark.asyncio
  async def test_validated_callable_raises_on_drift(self):
    def make() -> _Pair:
      return {'x': 1}  # type: ignore[return-value]  # simulate backend drift

    wrapped = validated_callable(self._tool(make))
    with pytest.raises(ValidationError):
      await wrapped()


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
    # namespacing happens at the assembling layer (ToolRegistry).
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
    with pytest.raises(
      UnknownToolError, match="unknown or disallowed tool: 'nonexistent'"
    ) as exception:
      await registry.call('nonexistent', {})
    assert exception.value.name == 'nonexistent'

  @pytest.mark.asyncio
  async def test_empty_registry(self):
    registry = ToolRegistry([])
    tools = await registry.resolve()
    assert tools == []


class TestRenderText:
  def test_harness_branches(self):
    text = (
      'watch: {{if #harness = bro}}job/watch{{else}}{{assert #harness = claude}}Monitor{{endif}}'
    )
    assert render_text(text, harness='bro') == 'watch: job/watch'
    assert render_text(text, harness='claude') == 'watch: Monitor'

  def test_wire_branches(self):
    text = 'call {{if #wire = bare}}ns__tool{{else}}{{assert #wire = mcp}}mcp__ns__tool{{endif}}'
    assert render_text(text, wire='bare') == 'call ns__tool'
    assert render_text(text, wire='mcp') == 'call mcp__ns__tool'

  def test_creds_membership_probes_availability(self, monkeypatch):
    monkeypatch.setattr(mcp_mod.credentials, 'available', lambda name: name == 'openai')
    text = '{{if openai ∈ #creds}}summarized{{else}}raw{{endif}}'
    assert render_text(text, creds=['openai']) == 'summarized'
    text = '{{if github ∈ #creds}}push{{else}}no push{{endif}}'
    assert render_text(text, creds=['openai', 'github']) == 'no push'

  def test_creds_outside_universe_raises(self, monkeypatch):
    monkeypatch.setattr(mcp_mod.credentials, 'available', lambda name: True)
    with pytest.raises(ValueError, match='universe'):
      render_text('{{if typo ∈ #creds}}x{{endif}}', creds=['openai'])

  def test_creds_probed_lazily(self, monkeypatch):
    # only the tested name resolves — a large universe costs nothing extra.
    probed: list[str] = []

    def available(name: str) -> bool:
      probed.append(name)
      return True

    monkeypatch.setattr(mcp_mod.credentials, 'available', available)
    render_text('{{if openai ∈ #creds}}x{{endif}}', creds=['openai', 'github', 'notion'])
    assert probed == ['openai']

  def test_absent_fact_raises_on_reference(self):
    with pytest.raises(ValueError, match='unknown variable #wire'):
      render_text('{{if #wire = bare}}x{{endif}}', harness='bro')

  def test_facts_combine(self):
    text = '{{if #harness = bro}}B{{endif}}{{if #wire = mcp}}M{{endif}}'
    assert render_text(text, harness='bro', wire='mcp') == 'BM'

  def test_plain_text_unchanged_without_consulting_availability(self, monkeypatch):
    def boom(name: str) -> bool:
      raise AssertionError('availability must not be consulted with no directive')

    monkeypatch.setattr(mcp_mod.credentials, 'available', boom)
    assert render_text('plain text', creds=[]) == 'plain text'

  def test_unknown_literal_raises(self):
    with pytest.raises(ValueError, match='domain'):
      render_text('{{if #harness = claud}}x{{endif}}', harness='claude')

  def test_unknown_harness_argument_raises(self):
    with pytest.raises(ValueError, match='unknown harness'):
      render_text('{{if a = a}}x{{endif}}', harness='gemini')  # type: ignore[arg-type]

  def test_unknown_wire_argument_raises(self):
    with pytest.raises(ValueError, match='unknown wire'):
      render_text('{{if a = a}}x{{endif}}', wire='grpc')  # type: ignore[arg-type]

  @pytest.mark.asyncio
  async def test_namespaced_tool_renders_description_against_availability(self, monkeypatch):
    def fetch(id: Annotated[str, Field(description='id')]) -> str:
      return id

    describe(fetch, 'fetch a record{{if openai ∈ #creds}}; summarized{{else}}; raw only{{endif}}')

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
