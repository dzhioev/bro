import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable


def describe[F: Callable[..., Any]](fn: F, text: str) -> F:
  fn.description = text  # type: ignore[attr-defined]
  return fn


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

  @property
  def output_schema(self) -> dict[str, Any] | None:
    return None

  @abstractmethod
  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str: ...


class MCPServer(ABC):
  @abstractmethod
  async def list_tools(self) -> list[Tool]: ...


class FunctionTool(Tool):
  def __init__(
    self,
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
  ):
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    resolved_description = (
      description if description is not None else getattr(fn, 'description', None)
    )
    if resolved_description is None:
      raise ValueError(
        f'tool {fn.__name__!r} has no description attribute and no description argument'
      )
    self._name = name if name is not None else fn.__name__
    self._description = resolved_description
    self.fn = fn

    ret = inspect.signature(fn).return_annotation
    structured = ret is not inspect.Signature.empty and ret is not str
    self._metadata = func_metadata(fn, structured_output=structured)
    self._output_schema = self._metadata.output_schema
    self._parameters = self._metadata.arg_model.model_json_schema(by_alias=True)

  @property
  def name(self) -> str:
    return self._name

  @property
  def description(self) -> str:
    return self._description

  @property
  def parameters(self) -> dict[str, Any]:
    return self._parameters

  @property
  def output_schema(self) -> dict[str, Any] | None:
    return self._output_schema

  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str:
    validated = self._metadata.arg_model.model_validate(self._metadata.pre_parse_json(arguments))
    kwargs = validated.model_dump_one_level()
    result = self.fn(**kwargs)
    if inspect.isawaitable(result):
      result = await result
    if self._output_schema is None:
      assert isinstance(result, str), (
        f'unstructured tool {self._name!r} must return str, got {type(result).__name__}'
      )
      return result
    assert self._metadata.output_model is not None
    if self._metadata.wrap_output:
      result = {'result': result}
    validated = self._metadata.output_model.model_validate(result)
    return validated.model_dump(mode='json', by_alias=True)


class InProcessMCPServer(MCPServer):
  def __init__(self, tools: Iterable[Tool]):
    self._tools = list(tools)

  async def list_tools(self) -> list[Tool]:
    return list(self._tools)


class FilteredMCPServer(MCPServer):
  def __init__(self, inner: MCPServer, allowed_tools: Iterable[str]):
    self._inner = inner
    self._allowed = list(allowed_tools)
    if len(self._allowed) == 0:
      raise ValueError('allowed_tools must be non-empty')

  async def list_tools(self) -> list[Tool]:
    by_name = {t.name: t for t in await self._inner.list_tools()}
    missing = [name for name in self._allowed if name not in by_name]
    if len(missing) > 0:
      raise ValueError(f'unknown tools in allowlist: {missing}; available: {sorted(by_name)}')
    return [by_name[name] for name in self._allowed]


class UnknownToolError(Exception):
  def __init__(self, name: str):
    super().__init__(f'unknown or disallowed tool: {name!r}')
    self.name = name


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

  async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | str:
    if self._tools_by_name is None:
      await self.resolve()
    assert self._tools_by_name is not None
    tool = self._tools_by_name.get(name)
    if tool is None:
      raise UnknownToolError(name)
    return await tool.call(arguments)
