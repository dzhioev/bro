import functools
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any, ClassVar, Literal, Optional, get_args, get_origin

from bro.base import condition, credentials, template
from bro.base.condition import var
from bro.base.offload import off_loop


def describe[F: Callable[..., Any]](function: F, text: str) -> F:
  function.description = text  # type: ignore[attr-defined]
  return function


# the agent harness a rendered text is consumed under — which toolset drives the
# work: `bro` is the bro toolset (native LLM runs and `--raw` claude sessions,
# where `--bare` strips claude's built-ins), `claude` is Claude Code's own
# harness with its built-in tools.
Harness = Literal['bro', 'claude']
_HARNESSES = frozenset(get_args(Harness))

# how a surface's tool list spells the canonical `namespace::tool` names: `bare`
# is the bro-native LLM loop's `namespace__tool`; `mcp` is any claude session's
# `mcp__namespace__tool` (each namespace mounted as an MCP server). orthogonal to
# `Harness` — a `--raw` session runs the bro harness over mcp wire names.
Wire = Literal['bare', 'mcp']
_WIRES = frozenset(get_args(Wire))

# the session's hold — its user-involvement level, ordered from no human
# channel to human-driven. unlike the other facts it is supplied only when
# rendering the hold text (`bro.prompts.hold_fragment`), so hold-neutral text —
# scripts, procedure docs — fails fast on a stray `#hold` directive.
Hold = Literal['unattended', 'detached', 'attended', 'guided']
HOLDS: tuple[str, ...] = get_args(Hold)
_HOLDS = frozenset(HOLDS)

# the facts triple as ready-made condition variables, so declarations read
# `harness == 'bro'` / `creds.contains('openai')`.
harness = var('harness')
wire = var('wire')
creds = var('creds')


def render_text(
  text: str,
  *,
  harness: Optional[Harness] = None,
  wire: Optional[Wire] = None,
  creds: Optional[Iterable[str]] = None,
  hold: Optional[str] = None,
  extra: Optional[condition.Variables] = None,
) -> str:
  """render `bro.base.template` directives in static agent-facing text (system
  prompts, script bodies, service-tool descriptions) against the surface facts
  the call site knows: `harness` → `#harness`, `wire` → `#wire`, `creds` →
  `#creds` (the closed universe; membership probes `credentials.available`
  lazily, so render in the process that consumes the text, where the store is
  the session's own), `hold` → `#hold` (hold text only — supplied by
  `bro.prompts.hold_fragment`, no other call site). A fact left None defines no
  variable, so a directive referencing it raises. `extra` merges a
  caller-owned domain vocabulary next to the facts (same shape as
  `FunctionTool`'s `variables`); its names shadow same-named facts.
  `{{include <name>}}` targets resolve through the `prompts` loader. The
  directive reference is `reference/template.md`. Ordinary MCP-server tool
  text does not use these facts: a server renders its own descriptions at
  build time against its own vocabulary (`FunctionTool`'s `variables`, e.g.
  the `#tools` roster).
  """
  if '{{' not in text:
    return text
  variables = surface_variables(harness=harness, wire=wire, creds=creds, hold=hold)
  if extra is not None:
    variables.update(extra)
  return template.render(text, variables, _load_prompt)


def _load_prompt(name: str) -> str:
  from bro import prompts  # lazy: keeps this layer import-free of the repo-root prompt store

  return prompts.get_prompt(name)


def select[T](
  entries: Iterable[condition.Entry[T]],
  *,
  harness: Optional[Harness] = None,
  wire: Optional[Wire] = None,
  creds: Optional[Iterable[str]] = None,
  extra: Optional[condition.Variables] = None,
) -> list[T]:
  """resolve the `bro.base.condition` wrappers (`when` / `iff`) in a declarative
  list against the same surface facts `render_text` renders with. a fact left
  None defines no variable, so a condition referencing it raises. `extra`
  merges a caller-owned domain vocabulary next to the facts, as in
  `render_text`. The conditioning reference is `reference/conditions.md`."""
  variables = surface_variables(harness=harness, wire=wire, creds=creds)
  if extra is not None:
    variables.update(extra)
  return condition.select(entries, variables)


def surface_variables(
  *,
  harness: Optional[Harness] = None,
  wire: Optional[Wire] = None,
  creds: Optional[Iterable[str]] = None,
  hold: Optional[str] = None,
) -> dict[str, condition.StringVariable | condition.SetVariable | bool]:
  """the harness facts as a `Variables` mapping — what `render_text` / `select`
  evaluate against. Public for the one tool surface allowed to condition on
  system facts: the bro service-tool build injects these (plus its roster
  vocabulary) into its tools' rendering variables."""
  variables: dict[str, condition.StringVariable | condition.SetVariable | bool] = {}
  if harness is not None:
    if harness not in _HARNESSES:
      raise ValueError(f'unknown harness {harness!r}; known: {", ".join(sorted(_HARNESSES))}')
    variables['harness'] = condition.StringVariable(harness, domain=_HARNESSES)
  if wire is not None:
    if wire not in _WIRES:
      raise ValueError(f'unknown wire scheme {wire!r}; known: {", ".join(sorted(_WIRES))}')
    variables['wire'] = condition.StringVariable(wire, domain=_WIRES)
  if creds is not None:
    variables['creds'] = condition.SetVariable(credentials.available, universe=frozenset(creds))
  if hold is not None:
    if hold not in _HOLDS:
      raise ValueError(f'unknown hold {hold!r}; known: {", ".join(HOLDS)}')
    variables['hold'] = condition.StringVariable(hold, domain=_HOLDS)
  return variables


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
  # prepends `mcp__`). `@` is outside provider wire charsets and spells `at` in
  # either segment.
  segments = ('at' if segment == '@' else segment for segment in (namespace, tool))
  return '__'.join(segments)


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


@dataclass(frozen=True)
class MCPServerSpec:
  """declarative manifest for an MCP server: its credential needs plus a builder.

  the declaration/runtime split: a spec is pure metadata — hosts read
  `needed_secrets` / `optional_secrets` from it before any credential exists
  (a bro's manifest, cw's container scoping) — while `build()` produces the
  live server and runs only in a serving process, so a server's constructor
  is free to hold real resources.
  """

  build: Callable[[], MCPServer]
  needed_secrets: tuple[str, ...] = ()
  optional_secrets: tuple[str, ...] = ()

  @staticmethod
  def of(server_cls: type[MCPServer], *args: Any, **kwargs: Any) -> 'MCPServerSpec':
    """spec for a server class that declares its secrets as class attributes.

    the escape hatch for irregularly-shaped servers; roster-based servers
    (a module-level list of tool functions) declare a `Toolset` instead.
    """
    return MCPServerSpec(
      build=functools.partial(server_cls, *args, **kwargs),
      needed_secrets=tuple(server_cls.needed_secrets),
      optional_secrets=tuple(server_cls.optional_secrets),
    )


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
    _validate_segment('namespace', namespace)
    self.namespace = namespace
    self._tools = list(tools)
    self._close = close
    for tool in self._tools:
      _validate_segment('tool name', tool.name)

  async def list_tools(self) -> list[Tool]:
    return list(self._tools)

  def close(self) -> None:
    if self._close is not None:
      self._close()


class Toolset[T]:
  """declarative definition of a roster-based in-process tool server.

  one instance per server module, conventionally named `spec` and defined above
  its tools, which register on it with the `@spec.tool('description')`
  decorator. Calling the instance with tool names validates the subset immediately
  at declaration time and
  returns the frozen `MCPServerSpec` manifest; `build()` runs later, in the
  serving process, constructing the per-server state once (`state` factory) and
  injecting it into every selected tool that declares a `Context` parameter.

  secrets: the base `get_secrets` returns the static `secrets` class var;
  subclass and override it when the credential set depends on the selected tools.
  """

  # credentials the toolset's tools read through the store when the set is
  # independent of the tool subset; the `get_secrets` default returns it.
  secrets: ClassVar[tuple[str, ...]] = ()

  def __init__(
    self,
    namespace: str,
    *,
    state: Callable[[], T] = lambda: None,
    close: Optional[Callable[[T], None]] = None,
  ):
    self.namespace = namespace
    self._by_name: dict[str, Callable[..., Any]] = {}
    self._state_factory = state
    # how a built server releases its state; `MCPServer.close` calls it once
    # when the session ends.
    self._close_state = close

  def tool[F: Callable[..., Any]](self, description: str) -> Callable[[F], F]:
    """register the decorated function as a tool and attach its description.

    returns the function unchanged, so it stays directly callable (tests call
    tools as plain functions). a duplicate name raises.
    """

    def register(function: F) -> F:
      if function.__name__ in self._by_name:
        raise ValueError(f'duplicate {self.namespace} tool: {function.__name__!r}')
      self._by_name[function.__name__] = describe(function, description)
      return function

    return register

  @property
  def tool_names(self) -> tuple[str, ...]:
    return tuple(self._by_name)

  def get_secrets(self, tool_names: Sequence[str]) -> tuple[str, ...]:
    """credentials needed by a server scoped to `tool_names`; default: the class var."""
    return self.secrets

  def _resolve(self, tool_names: tuple[str, ...]) -> tuple[str, ...]:
    """the full roster for no names; otherwise the given names, validated."""
    if len(tool_names) == 0:
      return tuple(self._by_name)
    unknown = [n for n in tool_names if n not in self._by_name]
    if len(unknown) > 0:
      raise ValueError(
        f'unknown {self.namespace} tools: {unknown}; available: {sorted(self._by_name)}'
      )
    return tool_names

  def _variables(self, selected: tuple[str, ...]) -> condition.Variables:
    # the toolset's rendering vocabulary: `#tools` — membership is this build's
    # selection, universe the full roster, so a description that tests an
    # unknown sibling raises at build.
    return {'tools': condition.SetVariable(frozenset(selected), universe=frozenset(self._by_name))}

  def tools(self, state: T) -> list[Tool]:
    """the full tool list bound to `state` — the seam tests inject fakes through."""
    names = tuple(self._by_name)
    variables = self._variables(names)
    return [
      FunctionTool(function, state=state, variables=variables)
      for function in self._by_name.values()
    ]

  def build(self, *tool_names: str) -> InProcessMCPServer:
    """the live server: per-server state built once, shared by every call through it."""
    names = self._resolve(tool_names)
    state = self._state_factory()
    variables = self._variables(names)
    close = None if self._close_state is None else functools.partial(self._close_state, state)
    server = InProcessMCPServer(
      self.namespace,
      [FunctionTool(self._by_name[n], state=state, variables=variables) for n in names],
      close=close,
    )
    # instance attributes over the writable class-attr defaults: the live
    # server stays self-describing — its scoped credential needs and the
    # definition roster it was built from.
    server.needed_secrets = self.get_secrets(names)
    server.tool_universe = tuple(self._by_name)
    return server

  def __call__(self, *tool_names: str) -> MCPServerSpec:
    names = self._resolve(tool_names)
    return MCPServerSpec(
      build=lambda: self.build(*names),
      needed_secrets=self.get_secrets(names),
    )


def as_spec(entry: 'MCPServerSpec | Toolset[Any] | ModuleType') -> MCPServerSpec:
  """a declaration entry normalized to its manifest: a tool-pack module is its
  conventional `spec` Toolset, a bare Toolset is its full roster, a spec passes
  through. Scoped subsets call the toolset with their selected names."""
  if isinstance(entry, ModuleType):
    toolset = getattr(entry, 'spec', None)
    if not isinstance(toolset, Toolset):
      raise TypeError(f'module {entry.__name__!r} declares no Toolset named spec')
    return toolset()
  if isinstance(entry, Toolset):
    return entry()
  return entry


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
