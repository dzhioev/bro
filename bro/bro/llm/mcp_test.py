import pytest
from typing import Annotated

from pydantic import Field

from llm.mcp import FunctionTool, InProcessMCPServer


class TestFunctionTool:
  def test_sync_function(self):
    def greet(name: Annotated[str, Field(description='person name')]) -> str:
      """say hello"""
      return f'hello {name}'

    tool = FunctionTool(greet)
    assert tool.name == 'greet'
    assert tool.description == 'say hello'
    assert 'name' in tool.parameters.get('properties', {})

  def test_custom_name_and_description(self):
    def greet(name: str) -> str:
      """original docstring"""
      return f'hello {name}'

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
      """add two numbers"""
      return str(a + b)

    tool = FunctionTool(add)
    result = await tool.call({'a': 3, 'b': 4})
    assert result == '7'

  @pytest.mark.asyncio
  async def test_call_async(self):
    async def upper(text: Annotated[str, Field(description='input text')]) -> str:
      """uppercase text"""
      return text.upper()

    tool = FunctionTool(upper)
    result = await tool.call({'text': 'hello'})
    assert result == 'HELLO'

  def test_optional_parameter(self):
    def search(
      query: Annotated[str, Field(description='search query')],
      limit: Annotated[int | None, Field(description='max results')] = None,
    ) -> str:
      """search for items"""
      return f'{query}:{limit}'

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
      """greet someone"""
      return f'{greeting} {name}'

    tool = FunctionTool(greet)
    result = await tool.call({'name': 'world'})
    assert result == 'hello world'

    result = await tool.call({'name': 'world', 'greeting': 'hi'})
    assert result == 'hi world'


class TestInProcessMCPServer:
  @pytest.mark.asyncio
  async def test_list_tools(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      """tool a"""
      return x

    def tool_b(y: Annotated[int, Field(description='number')]) -> str:
      """tool b"""
      return str(y)

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
      """tool a"""
      return x

    server = InProcessMCPServer([FunctionTool(tool_a)])
    tools1 = await server.list_tools()
    tools2 = await server.list_tools()
    assert tools1 is not tools2
    assert tools1[0] is tools2[0]
