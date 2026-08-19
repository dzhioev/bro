import functools
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Optional, get_origin

from bro import mcp
from bro.base import condition, template
from bro.base.offload import off_loop

_PRIMITIVE_SHAPE = {
  'string': 'str',
  'integer': 'int',
  'number': 'float',
  'boolean': 'bool',
  'null': 'null',
}


def render_return_shape(output_schema: dict[str, Any]) -> str:
  """render a tool's output JSON Schema as a readable shape.

  objects are pretty-printed across lines with 2-space indentation; scalars, enums,
  unions, and tuples stay inline. e.g. a list return renders as `Project{\\n  id: str,
  \\n  ...\\n}[]`. Meant to be appended to a tool description so an LLM knows the return
  shape without calling the tool first — clients ignore `outputSchema` unevenly and the
  upstream proxy historically rejected `structuredContent`, but every client reads the
  description.
  """
  defs = output_schema.get('$defs', {})
  return _render_shape(_unwrap_structured(output_schema), defs, frozenset(), 0)


def _unwrap_structured(schema: dict[str, Any]) -> dict[str, Any]:
  # func_metadata wraps a non-object return (list / Optional / scalar) in a synthetic
  # `{result: X}` object titled `<function>Output`; peel it so the shape reflects the real
  # return type rather than the wrapper.
  props = schema.get('properties')
  if (
    schema.get('type') == 'object'
    and isinstance(props, dict)
    and set(props.keys()) == {'result'}
    and schema.get('title', '').endswith('Output')
  ):
    return props['result']
  return schema


def _render_shape(
  node: dict[str, Any], defs: dict[str, Any], seen: frozenset[str], indent: int
) -> str:
  if '$ref' in node:
    name = node['$ref'].rsplit('/', 1)[-1]
    if name in seen:
      return name
    body = _render_shape(defs.get(name, {}), defs, seen | {name}, indent)
    # name a referenced object (`Project{...}`); leave inlined enums/scalars bare so
    # their allowed values show through.
    return f'{name}{body}' if body.startswith('{') else body
  if 'anyOf' in node:
    parts = [_render_shape(s, defs, seen, indent) for s in node['anyOf']]
    nullable = 'null' in parts
    non_null = [p for p in dict.fromkeys(parts) if p != 'null']
    if len(non_null) == 0:
      return 'null'
    rendered = '|'.join(non_null)
    return f'{rendered}|null' if nullable else rendered
  if 'enum' in node:
    return '|'.join(f'"{v}"' if isinstance(v, str) else str(v) for v in node['enum'])
  # a fixed-length tuple carries both `prefixItems` and `type: array`; match it first.
  if 'prefixItems' in node:
    return '[' + ', '.join(_render_shape(s, defs, seen, indent) for s in node['prefixItems']) + ']'
  node_type = node.get('type')
  if node_type == 'array':
    return f'{_render_shape(node.get("items", {}), defs, seen, indent)}[]'
  if node_type == 'object' or 'properties' in node:
    props = node.get('properties', {})
    if len(props) == 0:
      return '{}'
    pad = '  ' * (indent + 1)
    fields = ',\n'.join(
      f'{pad}{k}: {_render_shape(v, defs, seen, indent + 1)}' for k, v in props.items()
    )
    return '{\n' + fields + '\n' + '  ' * indent + '}'
  if node_type in _PRIMITIVE_SHAPE:
    return _PRIMITIVE_SHAPE[node_type]
  return 'any'


@dataclass(frozen=True)
class Context[T]:
  """per-call context a tool opts into by declaring a `Context`-annotated parameter.

  the envelope is built fresh for every call; `state` is the hosting server's
  long-lived per-server object, so tools reach
  persistent resources without global state. servers without state inject
  `Context[None]`. per-request fields (request id, timing) land here when the
  first consumer does.
  """

  state: T


def _context_parameter(function: Callable[..., Any]) -> Optional[str]:
  """name of the function's `Context`-annotated parameter, or None. detected by
  annotation (not by parameter name) so a rename can't silently stop the injection."""
  for name, parameter in inspect.signature(function, eval_str=True).parameters.items():
    ann = parameter.annotation
    if ann is Context or get_origin(ann) is Context:
      return name
  return None


class ToolControlSignal(Exception):
  """tool exception that must escape the LLM agent loop.

  the loop catches generic exceptions from a tool call and feeds them back to
  the model as the tool result, so the agent can react. tools that need to
  abort the run instead (a service-level signal like `raise`) must derive from
  this class.
  """


def render_tool_text(text: str, variables: condition.Variables) -> str:
  """render `bro.base.template` directives in a tool's static text against the
  owning server's rendering vocabulary (e.g. the `#tools` roster, a data
  source's `#features`). Servers render at build time, so no unprocessed
  directive ever leaves a server — with an empty vocabulary any directive that
  references a variable raises instead of leaking."""
  if '{{' not in text:
    return text
  return template.render(text, variables)


def render_schema_text(node: Any, variables: condition.Variables) -> Any:
  """`render_tool_text` over every `description` string of a JSON schema —
  parameter annotations are agent-facing text and render like the tool
  description they accompany."""
  if isinstance(node, dict):
    return {
      key: render_tool_text(value, variables)
      if key == 'description' and isinstance(value, str)
      else render_schema_text(value, variables)
      for key, value in node.items()
    }
  if isinstance(node, list):
    return [render_schema_text(item, variables) for item in node]
  return node


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


def wire_name(namespace: str, tool: str) -> str:
  # the harness-agnostic canonical name is `namespace::tool`; every harness that
  # actually runs the tool resolves `::` to `__` (Claude Code additionally
  # prepends `mcp__`).
  return f'{namespace}__{tool}'


def canonical_name(wire: str) -> str:
  # inverse of `wire_name`, for display surfaces; unambiguous because segments
  # never contain `__`. a name without a separator (an unnamespaced service
  # tool) passes through unchanged.
  namespace, separator, tool = wire.partition('__')
  if separator == '':
    return wire
  return f'{namespace}::{tool}'


class MCPServer(ABC):
  # credentials this server's tools resolve through the store. unioned across a
  # bro's declared servers (and along each server's own MRO) into
  # `bro.needed_secrets()` so the host can hydrate a scoped credential set per
  # bro. override with the secret names a subclass actually reads; the empty
  # default means "no credentials".
  needed_secrets: tuple[str, ...] = ()
  # credentials this server's tools use *if present* but degrade without (e.g. the
  # LLM key behind a query-focused summary). unioned into `bro.optional_secrets()`,
  # which the host hydrates best-effort (`build_scoped_store(optional=...)`) — an
  # absent one is skipped, not a launch failure. mirrors `needed_secrets`.
  optional_secrets: tuple[str, ...] = ()
  # the flat namespace this server's tools live in (`tasks`, `dev`, `bro`,
  # `<name>-source`). the assembling layer reads it to keep two sources'
  # identically-named tools (e.g. `search`) distinct — `ToolRegistry` forms
  # `namespace__tool` wire names; a per-namespace server host mounts it as the
  # endpoint. set by whatever builds the server.
  namespace: str
  # the full tool roster of the definition this server was built from, when the
  # build can scope to a subset (`Toolset.build`, the bro service server) — the
  # closed `#tools` universe its descriptions rendered against. None: the
  # server serves its whole definition.
  tool_universe: Optional[tuple[str, ...]] = None

  @abstractmethod
  async def list_tools(self) -> list[Tool]: ...

  def close(self) -> None:  # noqa: B027 — an opt-in hook, not a required override
    """release what the live server holds — processes it started, connections it
    opened. Called once when the session that built it ends; the default is a
    no-op, since most servers hold nothing."""


class FunctionTool(Tool):
  def __init__(
    self,
    function: Callable[..., Any],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    state: Any = None,
    variables: Optional[condition.Variables] = None,
  ):
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    resolved_description = (
      description if description is not None else getattr(function, 'description', None)
    )
    if resolved_description is None:
      raise ValueError(
        f'tool {function.__name__!r} has no description attribute and no description argument'
      )
    # `variables` is the owning server's rendering vocabulary; the description
    # and the parameter annotations render here, at construction, so the tool's
    # text leaves the server fully resolved.
    rendering_variables = variables if variables is not None else {}
    self._name = name if name is not None else function.__name__
    self._description = render_tool_text(resolved_description, rendering_variables)
    self.function = function
    # `state` is the hosting server's per-server object; a function that declares a
    # Context parameter gets it injected (wrapped in a fresh Context) on every
    # call, and the parameter is excluded from the derived input schema.
    self.state = state
    self.context_parameter = _context_parameter(function)

    skip = (self.context_parameter,) if self.context_parameter is not None else ()
    ret = inspect.signature(function).return_annotation
    structured = ret is not inspect.Signature.empty and ret is not str
    self._metadata = func_metadata(function, skip_names=skip, structured_output=structured)
    self._output_schema = self._metadata.output_schema
    self._parameters = render_schema_text(
      self._metadata.arg_model.model_json_schema(by_alias=True), rendering_variables
    )
    shape = render_return_shape(self._output_schema) if self._output_schema is not None else None
    # a shapeless `dict[str, Any]` return renders as a bare '{}' — it tells the
    # LLM nothing and reads as contradicting prose that describes the payload,
    # so the description carries no Returns line for it.
    self._return_shape = shape if shape != '{}' else None

  @property
  def name(self) -> str:
    return self._name

  @property
  def description(self) -> str:
    # append the return shape so an LLM knows the result structure without a probe call;
    # str-returning tools have no schema and are left as-is.
    if self._return_shape is None:
      return self._description
    return f'{self._description}\n\nReturns: {self._return_shape}'

  @property
  def parameters(self) -> dict[str, Any]:
    return self._parameters

  @property
  def output_schema(self) -> Optional[dict[str, Any]]:
    return self._output_schema

  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str:
    validated = self._metadata.arg_model.model_validate(self._metadata.pre_parse_json(arguments))
    kwargs = validated.model_dump_one_level()
    if self.context_parameter is not None:
      kwargs[self.context_parameter] = Context(state=self.state)
    if inspect.iscoroutinefunction(self.function):
      result = await self.function(**kwargs)
    else:
      # a blocking tool function would otherwise hold the loop for its whole
      # runtime. a cancelled call abandons the thread, so a tool that starts a
      # process is responsible for reaping it.
      result = await off_loop(functools.partial(self.function, **kwargs))
      if inspect.isawaitable(result):
        result = await result
    return self._coerce_output(result)

  def _coerce_output(self, result: Any) -> dict[str, Any] | str:
    # validate a raw return against the output schema and return the JSON-ready value: a
    # str-returning tool passes through; a structured one is model-validated (raising on a
    # backend result that doesn't match the declared shape) and dumped to canonical JSON.
    if self._output_schema is None:
      assert isinstance(result, str), (
        f'unstructured tool {self._name!r} must return str, got {type(result).__name__}'
      )
      return result
    assert self._metadata.output_model is not None
    payload = {'result': result} if self._metadata.wrap_output else result
    return self._metadata.output_model.model_validate(payload).model_dump(
      mode='json', by_alias=True
    )

  def validate_output(self, result: Any) -> None:
    """raise if a raw tool return doesn't conform to the output schema; no-op otherwise.

    for delivery channels that serialize the result themselves (the HTTP MCP server) but
    still want the in-process path's fail-fast guard against backend drift. The validated
    JSON it produces is discarded — only the raising side effect is wanted.
    """
    self._coerce_output(result)


def validated_callable(tool: FunctionTool) -> Callable[..., Any]:
  """wrap a FunctionTool's raw function so its return is schema-validated before use.

  for a delivery channel that registers the bare function with an external framework and
  serializes the result itself (the HTTP MCP server via FastMCP): `functools.wraps` keeps
  the original signature, so the framework derives the same input schema, while the wrapper
  adds the in-process path's fail-fast check that the backend result matches the declared
  output shape. The result is returned unchanged for the framework to serialize.
  """
  function = tool.function
  context_parameter = tool.context_parameter

  @functools.wraps(function)
  async def validating(**kwargs: Any) -> Any:
    if context_parameter is not None:
      kwargs[context_parameter] = Context(state=tool.state)
    result = function(**kwargs)
    if inspect.isawaitable(result):
      result = await result
    tool.validate_output(result)
    return result

  if context_parameter is not None:
    # hide the injected parameter from the framework's schema derivation: an
    # explicit __signature__ wins over the __wrapped__ chain functools.wraps sets up.
    signature = inspect.signature(function)
    validating.__signature__ = signature.replace(  # pyright: ignore[reportAttributeAccessIssue]
      parameters=[p for p in signature.parameters.values() if p.name != context_parameter]
    )
  return validating


class _NamespacedTool(Tool):
  """wraps a tool to advertise its `namespace__tool` wire name.

  the underlying tool keeps its local `name` (the in-namespace identity, and what
  surfaces that namespace externally — an HTTP MCP server or Claude Code's
  `mcp__<namespace>__` mount — advertise); the assembling layer wraps it so the bro LLM sees,
  and calls back with, the namespaced wire name.
  """

  def __init__(self, namespace: str, tool: Tool):
    self._wire_name = wire_name(namespace, tool.name)
    self._tool = tool

  @property
  def name(self) -> str:
    return self._wire_name

  @property
  def description(self) -> str:
    return self._tool.description

  @property
  def parameters(self) -> dict[str, Any]:
    return self._tool.parameters

  @property
  def output_schema(self) -> Optional[dict[str, Any]]:
    return self._tool.output_schema

  async def call(self, arguments: dict[str, Any]) -> dict[str, Any] | str:
    return await self._tool.call(arguments)


async def namespaced_tools(server: MCPServer) -> list[Tool]:
  # a server's tools wrapped with their `namespace__tool` wire names — the
  # assembling step for a harness that flattens tools across servers into one
  # list (`ToolRegistry`), which adds its own collision policy on top.
  return [_NamespacedTool(server.namespace, tool) for tool in await server.list_tools()]


class InProcessMCPServer(MCPServer):
  def __init__(
    self, namespace: str, tools: Iterable[Tool], *, close: Optional[Callable[[], None]] = None
  ):
    mcp.validate_segment('namespace', namespace)
    self.namespace = namespace
    self._tools = list(tools)
    self._close = close
    for tool in self._tools:
      mcp.validate_segment('tool name', tool.name)

  async def list_tools(self) -> list[Tool]:
    return list(self._tools)

  def close(self) -> None:
    if self._close is not None:
      self._close()


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
