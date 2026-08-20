from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional, get_args

from bro.base import condition, credentials, template
from bro.base.condition import var

if TYPE_CHECKING:
  from bro.llm.mcp import InProcessMCPServer, MCPServer, Tool


def describe[F: Callable[..., Any]](function: F, text: str) -> F:
  function.description = text  # type: ignore[attr-defined]
  return function


def validate_segment(kind: str, value: str) -> None:
  if len(value) == 0:
    raise ValueError(f'{kind} must be non-empty')
  if '__' in value:
    raise ValueError(
      f'{kind} {value!r} contains a double underscore; "__" is reserved as the '
      'namespace/tool separator (single "_" and "-" are allowed)'
    )


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
# spells, procedure docs — fails fast on a stray `#hold` directive.
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
  prompts, spell bodies, service-tool descriptions) against the surface facts
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


@dataclass(frozen=True)
class MCPServerSpec:
  """declarative manifest for an MCP server: its credential needs plus a builder.

  the declaration/runtime split: a spec is pure metadata — hosts read
  `needed_secrets` / `optional_secrets` from it before any credential exists
  (a bro's manifest, ride's container scoping) — while `build()` produces the
  live server and runs only in a serving process, so a server's constructor
  is free to hold real resources.
  """

  build: Callable[[], MCPServer]
  needed_secrets: tuple[str, ...] = ()
  optional_secrets: tuple[str, ...] = ()

  @staticmethod
  def of(server_cls: type[MCPServer], *args: Any, **kwargs: Any) -> MCPServerSpec:
    """spec for a server class that declares its secrets as class attributes.

    the escape hatch for irregularly-shaped servers; roster-based servers
    (a module-level list of tool functions) declare a `Toolset` instead.
    """
    return MCPServerSpec(
      build=functools.partial(server_cls, *args, **kwargs),
      needed_secrets=tuple(server_cls.needed_secrets),
      optional_secrets=tuple(server_cls.optional_secrets),
    )


def _validate_native_names(names: object, field: str, verb: str) -> None:
  if not isinstance(names, tuple) or any(
    not isinstance(name, str) or len(name) == 0 for name in names
  ):
    raise TypeError(f'{field} must be a tuple of non-empty strings')
  if len(set(names)) != len(names):
    raise ValueError(f'a tool layer {verb} duplicate names: {names!r}')


@dataclass(frozen=True)
class ToolLayer:
  """one composable layer of server mounts and harness-native tool blocks."""

  server_specs: tuple[MCPServerSpec, ...] = ()
  blocked_native_tool_names: tuple[str, ...] = ()
  # `(tool name, command)` pairs narrowing a harness-native tool that takes a
  # command to run: the tool is served, and the harness rejects every call whose
  # command is not one of these
  native_tool_commands: tuple[tuple[str, str], ...] = ()
  # harness-native tools served whole over a block, their calls unrestricted
  served_native_tool_names: tuple[str, ...] = ()

  def __post_init__(self) -> None:
    if not isinstance(self.server_specs, tuple) or any(
      not isinstance(spec, MCPServerSpec) for spec in self.server_specs
    ):
      raise TypeError('server_specs must be a tuple of MCPServerSpec values')
    _validate_native_names(self.blocked_native_tool_names, 'blocked_native_tool_names', 'blocks')
    _validate_native_names(self.served_native_tool_names, 'served_native_tool_names', 'serves')
    pairs = self.native_tool_commands
    if not isinstance(pairs, tuple) or any(
      not isinstance(value, str) or len(value) == 0 for pair in pairs for value in pair
    ):
      raise TypeError('native_tool_commands must be a tuple of non-empty (name, command) pairs')
    if (
      len(self.server_specs) == 0
      and len(self.blocked_native_tool_names) == 0
      and len(pairs) == 0
      and len(self.served_native_tool_names) == 0
    ):
      raise ValueError(
        'a tool layer must mount a server, block a native tool, narrow one, or serve one'
      )

  def __or__(self, other: ToolLayer) -> ToolLayer:
    """both layers' declarations as one layer."""
    return ToolLayer(
      server_specs=self.server_specs + other.server_specs,
      blocked_native_tool_names=self.blocked_native_tool_names + other.blocked_native_tool_names,
      native_tool_commands=self.native_tool_commands + other.native_tool_commands,
      served_native_tool_names=self.served_native_tool_names + other.served_native_tool_names,
    )


def mount(toolset: Toolset[Any], *tool_names: str) -> ToolLayer:
  if not isinstance(toolset, Toolset):
    raise TypeError('mount requires a Toolset')
  return ToolLayer(server_specs=(toolset._manifest(*tool_names),))


def block(*tool_names: str) -> ToolLayer:
  return ToolLayer(blocked_native_tool_names=tool_names)


def allow_commands(tool_name: str, *commands: str) -> ToolLayer:
  """serve the harness-native `tool_name`, reaching only `commands`.

  For a native tool whose argument is a command line to run: the harness admits
  a call whose command is exactly one of `commands` and rejects the rest, so a
  persona reaches what it declares and no more — `sh`'s bargain, for a tool the
  harness serves rather than this layer.
  """
  if len(commands) == 0:
    raise ValueError(f'narrowing {tool_name} needs at least one command')
  return ToolLayer(native_tool_commands=tuple((tool_name, command) for command in commands))


def serve(*tool_names: str) -> ToolLayer:
  """serve the harness-native `tool_names` whole, over a block that withholds them.

  `allow_commands` without the narrowing, for a tool whose argument is not a
  command line. Like a narrowing, it must name a tool the bro also blocks.
  """
  if len(tool_names) == 0:
    raise ValueError('serving needs at least one tool name')
  return ToolLayer(served_native_tool_names=tool_names)


_COMMAND_WORD = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')


def sh(command: str, *argument_names: str) -> ToolLayer:
  """serve one installed CLI command as a generated tool.

  `command` is a program name and any subcommands (`'bro list'`); the tool's
  signature is derived from the command's own argument declarations when the
  server is built, and `argument_names` narrows the exposure to the named
  arguments, defaulting to all of them. Nothing is imported or run here — a
  command that cannot be read, or a required argument withheld from the
  exposure, fails at build.

  The command runs as a fixed argv with no shell between, so a bro reaches
  exactly what it declares. Credentials the command reads are the declaring
  bro's `extra_secrets`: this layer needs none of its own.
  """
  words = tuple(command.split())
  if len(words) == 0:
    raise ValueError('sh needs a command')
  for word in words:
    if _COMMAND_WORD.fullmatch(word) is None:
      raise ValueError(
        f'{word!r} is not a command word; a declaration names a program and its '
        'subcommands, and nothing a shell would interpret'
      )
  name = '_'.join(words).replace('.', '_')
  validate_segment('tool name', name)

  def build() -> MCPServer:
    from bro.llm import cli_tool

    return cli_tool.build_server(words, argument_names, name)

  return ToolLayer(server_specs=(MCPServerSpec(build=build),))


class Toolset[T]:
  """declarative definition of a roster-based in-process tool server.

  one instance per server module, conventionally named `toolset` and defined
  above its tools, which register on it with the `@toolset.tool('description')`
  decorator. `mount(toolset, *tool_names)` validates the subset immediately and
  returns a frozen `ToolLayer`; `build()` runs later, in the serving process,
  constructing the per-server state once (`state` factory) and injecting it
  into every selected tool that declares a `Context` parameter.

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
    from bro.llm.mcp import FunctionTool

    names = tuple(self._by_name)
    variables = self._variables(names)
    return [
      FunctionTool(function, state=state, variables=variables)
      for function in self._by_name.values()
    ]

  def build(self, *tool_names: str) -> InProcessMCPServer:
    """the live server: per-server state built once, shared by every call through it."""
    from bro.llm.mcp import FunctionTool, InProcessMCPServer

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

  def _manifest(self, *tool_names: str) -> MCPServerSpec:
    names = self._resolve(tool_names)
    return MCPServerSpec(
      build=lambda: self.build(*names),
      needed_secrets=self.get_secrets(names),
    )
