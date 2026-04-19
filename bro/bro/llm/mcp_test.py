import pytest
from typing import Annotated

from pydantic import Field

from llm.mcp import FunctionTool, InProcessMCPServer, ToolRegistry, describe


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
      limit: Annotated[int | None, Field(description='max results')] = None,
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


class TestInProcessMCPServer:
  @pytest.mark.asyncio
  async def test_list_tools(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    def tool_b(y: Annotated[int, Field(description='number')]) -> str:
      return str(y)

    describe(tool_b, 'tool b')

    server = InProcessMCPServer([FunctionTool(tool_a), FunctionTool(tool_b)])
    tools = await server.list_tools()
    assert len(tools) == 2
    assert tools[0].name == 'tool_a'
    assert tools[1].name == 'tool_b'

  @pytest.mark.asyncio
  async def test_empty_server(self):
    server = InProcessMCPServer([])
    tools = await server.list_tools()
    assert tools == []

  @pytest.mark.asyncio
  async def test_list_tools_returns_copy(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    server = InProcessMCPServer([FunctionTool(tool_a)])
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

    registry = ToolRegistry([InProcessMCPServer([FunctionTool(tool_a)])])
    tools = await registry.resolve()
    assert len(tools) == 1
    assert tools[0].name == 'tool_a'

  @pytest.mark.asyncio
  async def test_resolve_from_multiple_servers(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    def tool_b(y: Annotated[int, Field(description='number')]) -> str:
      return str(y)

    describe(tool_b, 'tool b')

    server_a = InProcessMCPServer([FunctionTool(tool_a)])
    server_b = InProcessMCPServer([FunctionTool(tool_b)])
    registry = ToolRegistry([server_a, server_b])
    tools = await registry.resolve()
    assert len(tools) == 2
    names = {t.name for t in tools}
    assert names == {'tool_a', 'tool_b'}

  @pytest.mark.asyncio
  async def test_resolve_caches(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    registry = ToolRegistry([InProcessMCPServer([FunctionTool(tool_a)])])
    tools1 = await registry.resolve()
    tools2 = await registry.resolve()
    assert tools1[0] is tools2[0]

  @pytest.mark.asyncio
  async def test_duplicate_name_raises(self):
    def dupe(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(dupe, 'duplicate')

    server_a = InProcessMCPServer([FunctionTool(dupe)])
    server_b = InProcessMCPServer([FunctionTool(dupe)])
    registry = ToolRegistry([server_a, server_b])
    with pytest.raises(ValueError, match='duplicate tool name'):
      await registry.resolve()

  @pytest.mark.asyncio
  async def test_call_by_name(self):
    def reverse(text: Annotated[str, Field(description='text')]) -> str:
      return text[::-1]

    describe(reverse, 'reverse text')

    registry = ToolRegistry([InProcessMCPServer([FunctionTool(reverse)])])
    result = await registry.call('reverse', {'text': 'hello'})
    assert result == 'olleh'

  @pytest.mark.asyncio
  async def test_call_unknown_raises(self):
    registry = ToolRegistry([InProcessMCPServer([])])
    with pytest.raises(KeyError, match='unknown tool'):
      await registry.call('nonexistent', {})

  @pytest.mark.asyncio
  async def test_empty_registry(self):
    registry = ToolRegistry([])
    tools = await registry.resolve()
    assert tools == []
