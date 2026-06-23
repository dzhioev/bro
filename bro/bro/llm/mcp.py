import inspect
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import Any, Optional

from base import credentials


def describe[F: Callable[..., Any]](fn: F, text: str) -> F:
  fn.description = text  # type: ignore[attr-defined]
  return fn


# `{{#has_cred <name>}}present{{else}}absent{{/has_cred}}` (and inverted `^`)
# blocks in a static description string. block-delimited and non-nested (the
# `.*?` is lazy and a body may not itself contain another block), so the `{{ }}`
# fences can't collide with literal text. rendered by `render_has_cred`.
_HAS_CRED_RE = re.compile(
  r'\{\{(?P<kind>[#^])has_cred\s+(?P<name>[A-Za-z0-9_]+)\}\}(?P<body>.*?)\{\{/has_cred\}\}',
  re.DOTALL,
)


def render_has_cred(text: str, available: Callable[[str], bool], declared: Iterable[str]) -> str:
  """render `{{#has_cred <name>}}…{{else}}…{{/has_cred}}` blocks in a description.

  a `#` block keeps its present branch when `available(name)`, else its `{{else}}`
  branch (empty when there is no `{{else}}`); an inverted `^` block keeps its body
  only when the secret is NOT available (no `{{else}}`). a string with no block is
  returned unchanged and `available` is never consulted. `name` must be one of
  `declared` (the component's needed + optional secrets) — a typo would otherwise
  silently render the absent branch forever, so it raises.
  """
  declared_set = set(declared)

  def replace(m: re.Match) -> str:
    name = m.group('name')
    if name not in declared_set:
      listing = ', '.join(sorted(declared_set)) if len(declared_set) > 0 else '(none)'
      raise ValueError(f'has_cred references undeclared secret {name!r}; declared: {listing}')
    is_available = available(name)
    if m.group('kind') == '^':
      # inverted: the whole body renders only when the secret is absent; no else.
      return '' if is_available else m.group('body')
    present, _, otherwise = m.group('body').partition('{{else}}')
    return present if is_available else otherwise

  return _HAS_CRED_RE.sub(replace, text)


class ToolControlSignal(Exception):
  """tool exception that must escape the LLM agent loop.

  the loop catches generic exceptions from a tool call and feeds them back to
  the model as the tool result, so the agent can react. tools that need to
  abort the run instead (a service-level signal like `raise`) must derive from
  this class.
  """


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
  def output_schema(self) -> Optional[dict[str, Any]]:
    return None

  @abstractmethod
  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str: ...


def _validate_segment(kind: str, value: str) -> None:
  # `__` is the namespace/tool wire separator (`ns__tool`); a segment that
  # contains it would break round-tripping. single `_` and `-` are fine.
  if len(value) == 0:
    raise ValueError(f'{kind} must be non-empty')
  if '__' in value:
    raise ValueError(
      f'{kind} {value!r} contains a double underscore; "__" is reserved as the '
      'namespace/tool separator (single "_" and "-" are allowed)'
    )


def wire_name(namespace: str, tool: str) -> str:
  # the harness-agnostic canonical name is `namespace::tool`; every harness that
  # actually runs the tool resolves `::` to `__` (Claude Code additionally
  # prepends `mcp__`). this builds the `__` wire name the bro LLM sees and calls.
  return f'{namespace}__{tool}'


class MCPServer(ABC):
  # credentials this server's tools resolve through the store. unioned across a
  # bro's declared servers (and along each server's own MRO) into
  # `bro.needed_secrets()` so the host can hydrate a scoped credential set per
  # bro. override with the secret names a subclass actually reads (e.g. flow →
  # `notion`); the empty default means "no credentials".
  needed_secrets: tuple[str, ...] = ()
  # credentials this server's tools use *if present* but degrade without (e.g. the
  # LLM key behind a query-focused summary). unioned into `bro.optional_secrets()`,
  # which the host hydrates best-effort (`build_scoped_store(optional=...)`) — an
  # absent one is skipped, not a launch failure. mirrors `needed_secrets`.
  optional_secrets: tuple[str, ...] = ()
  # the flat namespace this server's tools live in (`flow`, `dev`, `infra`,
  # `bro`, `<name>-source`). the assembling layer (`ToolRegistry` /
  # `mcp_server._Aggregate`) reads it to form `namespace__tool` wire names and to
  # keep two sources' identically-named tools (e.g. `search`) distinct. set by
  # whatever builds the server.
  namespace: str

  @abstractmethod
  async def list_tools(self) -> list[Tool]: ...


class FunctionTool(Tool):
  def __init__(
    self,
    fn: Callable[..., Any],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
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
  def output_schema(self) -> Optional[dict[str, Any]]:
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


class _NamespacedTool(Tool):
  """wraps a tool to advertise its `namespace__tool` wire name.

  the underlying tool keeps its local `name` (the in-namespace identity, and what
  surfaces that namespace externally — the flow HTTP server, Claude Code's
  `mcp__flow__` — advertise); the assembling layer wraps it so the bro LLM sees,
  and calls back with, the namespaced wire name.
  """

  def __init__(self, namespace: str, tool: Tool, declared_secrets: Iterable[str] = ()):
    self._wire_name = wire_name(namespace, tool.name)
    self._tool = tool
    self._declared_secrets = tuple(declared_secrets)

  @property
  def name(self) -> str:
    return self._wire_name

  @property
  def description(self) -> str:
    # render any `has_cred` blocks against live credential availability — the
    # one place that covers every assembled tool (bro LLM + deployed MCP servers).
    return render_has_cred(self._tool.description, credentials.available, self._declared_secrets)

  @property
  def parameters(self) -> dict[str, Any]:
    return self._tool.parameters

  @property
  def output_schema(self) -> Optional[dict[str, Any]]:
    return self._tool.output_schema

  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str:
    return await self._tool.call(arguments)


async def namespaced_tools(server: MCPServer) -> list[Tool]:
  # a server's tools wrapped with their `namespace__tool` wire names — the shared
  # step for every layer that assembles tools across servers for a harness
  # (`ToolRegistry`, `mcp_server._Aggregate`). each caller adds its own
  # collision policy on top. the server's declared secrets (needed + optional)
  # ride along so each tool's description can resolve its `has_cred` blocks.
  declared = set(server.needed_secrets) | set(server.optional_secrets)
  return [_NamespacedTool(server.namespace, tool, declared) for tool in await server.list_tools()]


class InProcessMCPServer(MCPServer):
  def __init__(self, namespace: str, tools: Iterable[Tool]):
    _validate_segment('namespace', namespace)
    self.namespace = namespace
    self._tools = list(tools)
    for tool in self._tools:
      _validate_segment('tool name', tool.name)

  async def list_tools(self) -> list[Tool]:
    return list(self._tools)


class UnknownToolError(Exception):
  def __init__(self, name: str):
    super().__init__(f'unknown or disallowed tool: {name!r}')
    self.name = name


class ToolRegistry:
  def __init__(self, mcp_servers: list[MCPServer]):
    self._mcp_servers: list[MCPServer] = list(mcp_servers)
    self._tools_by_name: Optional[dict[str, Tool]] = None

  async def resolve(self) -> list[Tool]:
    if self._tools_by_name is not None:
      return list(self._tools_by_name.values())
    tools_by_name: dict[str, Tool] = {}
    for server in self._mcp_servers:
      for wrapped in await namespaced_tools(server):
        if wrapped.name in tools_by_name:
          raise ValueError(
            f'duplicate tool wire name across MCP servers: {wrapped.name} '
            f'(namespace {server.namespace!r})'
          )
        tools_by_name[wrapped.name] = wrapped
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
