import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Iterable, cast


class Tool(ABC):
  @property
  @abstractmethod
  def name(self) -> str: ...

  @property
  @abstractmethod
  def description(self) -> str: ...

  @property
  @abstractmethod
  def parameters(self) -> dict[str, Any]: ...

  @abstractmethod
  async def call(self, arguments: dict[str, Any]) -> str: ...


class MCPServer(ABC):
  @abstractmethod
  async def list_tools(self) -> list[Tool]: ...


class FunctionTool(Tool):
  def __init__(
    self,
    fn: Callable[..., str | Awaitable[str]],
    *,
    name: str | None = None,
    description: str | None = None,
  ):
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    resolved_description = description if description is not None else inspect.getdoc(fn)
    if not resolved_description:
      raise ValueError(f'tool {fn.__name__!r} has no description and no docstring')
    self._name = name if name is not None else fn.__name__
    self._description = resolved_description
    self.fn = fn
    self._parameters = func_metadata(fn).arg_model.model_json_schema(by_alias=True)

  @property
  def name(self) -> str:
    return self._name

  @property
  def description(self) -> str:
    return self._description

  @property
  def parameters(self) -> dict[str, Any]:
    return self._parameters

  async def call(self, arguments: dict[str, Any]) -> str:
    result: str | Awaitable[str] = self.fn(**arguments)
    if isinstance(result, str):
      return result
    return await cast(Awaitable[str], result)


class InProcessMCPServer(MCPServer):
  def __init__(self, tools: Iterable[Tool]):
    self._tools = list(tools)

  async def list_tools(self) -> list[Tool]:
    return list(self._tools)


class ToolRegistry:
  def __init__(self, mcp_servers: list[MCPServer]):
    self._mcp_servers: list[MCPServer] = list(mcp_servers)
    self._tools_by_name: dict[str, Tool] | None = None

  async def resolve(self) -> list[Tool]:
    if self._tools_by_name is not None:
      return list(self._tools_by_name.values())
    tools_by_name: dict[str, Tool] = {}
    for server in self._mcp_servers:
      for tool in await server.list_tools():
        if tool.name in tools_by_name:
          raise ValueError(f'duplicate tool name across MCP servers: {tool.name}')
        tools_by_name[tool.name] = tool
    self._tools_by_name = tools_by_name
    return list(tools_by_name.values())

  async def call(self, name: str, arguments: dict[str, Any]) -> str:
    if self._tools_by_name is None:
      await self.resolve()
    assert self._tools_by_name is not None
    tool = self._tools_by_name.get(name)
    if tool is None:
      raise KeyError(f'unknown tool: {name}')
    return await tool.call(arguments)
