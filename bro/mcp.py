from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional, cast, get_args

from bro.base import condition, credentials, template
from bro.base.condition import var
from bro.harness import Harness, harness_name, name_of

if TYPE_CHECKING:
  from bro.datasources.base import DataSource
  from bro.datasources.file import FileSource
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


type HarnessLike = Harness | str

# the session's hold — its user-involvement level, ordered from no human
# channel to human-driven.
Hold = Literal['unattended', 'detached', 'attended', 'guided']
HOLDS: tuple[str, ...] = get_args(Hold)
_HOLDS = frozenset(HOLDS)
_FRAMEWORK_FACT_NAMES = frozenset({'harness', 'creds', 'may_summon', 'talk', 'hold'})

# the credential fact as a ready-made condition variable, so a declaration's
# gate reads `creds.contains('openai')`.
creds = var('creds')


def render_text(
  text: str,
  *,
  harness: Optional[HarnessLike] = None,
  creds: Optional[Iterable[str]] = None,
  may_summon: Optional[Iterable[str]] = None,
  talk: Optional[Iterable[str]] = None,
  hold: Optional[str] = None,
  extra: Optional[condition.Variables] = None,
) -> str:
  """render `bro.base.template` directives in static agent-facing text (system
  prompts, spell bodies, service-tool descriptions) against the surface facts
  the call site knows: a `Harness` object contributes its own facts and its name as
  `#harness`; `creds` →
  `#creds` (the closed universe; membership probes `credentials.available`
  lazily, so render in the process that consumes the text, where the store is
  the session's own), `may_summon` → `#may_summon` (the session's effective
  `launch.bro.bros` set — `bro.summon.effective_may_summon()`; membership is is-a,
  so a granted bro answers to the bros it derives from (`registry.lineage`) as
  well as to its own name, and the universe adds the installed persona names,
  so a granted-but-uninstalled target still tests), `talk` → `#talk` (the fixed
  rights of this run's own quest), `hold` → `#hold` (the session's hold, one of
  `HOLDS`). A fact
  left None defines no variable, so a directive referencing it raises. `extra`
  merges a caller-owned domain vocabulary next to the facts (same shape as
  `FunctionTool`'s `variables`); its names shadow same-named facts.
  `{{include <name>}}` targets resolve through the `prompts` loader. The
  directive reference is `reference/template.md`. Ordinary MCP-server tool
  text does not use these facts: a server renders its own descriptions at
  build time against its own vocabulary (`FunctionTool`'s `variables`, e.g.
  the `#tools` roster).
  """
  if '{{' not in text:
    return text
  variables = surface_variables(
    harness=harness, creds=creds, may_summon=may_summon, talk=talk, hold=hold
  )
  if extra is not None:
    variables.update(extra)
  return template.render(text, variables, _load_prompt)


def _load_prompt(name: str) -> str:
  from bro import prompts  # lazy: keeps this layer import-free of the repo-root prompt store

  return prompts.get_prompt(name)


def select[T](
  entries: Iterable[condition.Entry[T]],
  *,
  harness: Optional[HarnessLike] = None,
  creds: Optional[Iterable[str]] = None,
  may_summon: Optional[Iterable[str]] = None,
  talk: Optional[Iterable[str]] = None,
  extra: Optional[condition.Variables] = None,
) -> list[T]:
  """resolve the `bro.base.condition` wrappers (`when` / `iff`) in a declarative
  list against the same surface facts `render_text` renders with. a fact left
  None defines no variable, so a condition referencing it raises. `extra`
  merges a caller-owned domain vocabulary next to the facts, as in
  `render_text`. The conditioning reference is `reference/conditions.md`."""
  variables = surface_variables(harness=harness, creds=creds, may_summon=may_summon, talk=talk)
  if extra is not None:
    variables.update(extra)
  return condition.select(entries, variables)


@functools.cache
def _persona_names() -> frozenset[str]:
  # lazy: keeps this layer import-free of the bro class graph (the registry
  # reads entry-point metadata only, importing no bro module)
  from bro import registry

  return frozenset(registry.declared_specs())


def _answers_to(granted: frozenset[str]) -> Callable[[str], bool]:
  """`#may_summon`'s membership probe: a granted bro answers to the bros it
  derives from as well as to its own name."""

  def probe(name: str) -> bool:
    if name in granted:
      return True
    # lazy: resolving a lineage imports the granted bro's module, which a text
    # that never tests the fact must not pay for
    from bro import registry

    return any(name in registry.lineage(target) for target in granted)

  return probe


def surface_variables(
  *,
  harness: Optional[HarnessLike] = None,
  creds: Optional[Iterable[str]] = None,
  may_summon: Optional[Iterable[str]] = None,
  talk: Optional[Iterable[str]] = None,
  hold: Optional[str] = None,
) -> dict[str, condition.StringVariable | condition.SetVariable | bool]:
  """surface facts as a `Variables` mapping — what `render_text` / `select`
  evaluate against, for a caller that evaluates a condition against them
  directly or merges them into a vocabulary of its own. A `Harness` object
  contributes its prompt vocabulary in addition to the framework facts."""
  variables: dict[str, condition.StringVariable | condition.SetVariable | bool] = {}
  if harness is not None:
    if isinstance(harness, Harness):
      harness_facts = harness.facts()
      conflicts = sorted(harness_facts.keys() & _FRAMEWORK_FACT_NAMES)
      if len(conflicts) > 0:
        raise ValueError(
          f'harness {harness.name!r} facts conflict with framework facts: {", ".join(conflicts)}'
        )
      variables.update(harness_facts)
    variables['harness'] = condition.StringVariable(
      name_of(harness), literal_validator=harness_name
    )
  if creds is not None:
    variables['creds'] = condition.SetVariable(credentials.available, universe=frozenset(creds))
  if may_summon is not None:
    granted = frozenset(may_summon)
    variables['may_summon'] = condition.SetVariable(
      _answers_to(granted), universe=granted | _persona_names()
    )
  if talk is not None:
    from bro.broker.brotocol import TALK_RIGHTS

    rights = frozenset(talk)
    unknown = sorted(rights - TALK_RIGHTS)
    if len(unknown) > 0:
      raise ValueError(f'unknown talk right(s): {", ".join(unknown)}')
    variables['talk'] = condition.SetVariable(rights.__contains__, universe=TALK_RIGHTS)
  if hold is not None:
    if hold not in _HOLDS:
      raise ValueError(f'unknown hold {hold!r}; known: {", ".join(HOLDS)}')
    variables['hold'] = condition.StringVariable(hold, domain=_HOLDS)
  return variables


@dataclass(frozen=True)
class MCPServerSpec:
  """declarative manifest for an MCP server: the namespace it serves, its
  credential needs, and a builder.

  the declaration/runtime split: a spec is pure metadata — hosts read
  `needed_secrets` / `optional_secrets` from it before any credential exists
  (a bro's manifest, ride's container scoping) — while `build()` produces the
  live server and runs only in a serving process, so a server's constructor
  is free to hold real resources. `namespace` is known before anything is
  built, and the server `build()` returns serves it.
  """

  namespace: str
  build: Callable[[], MCPServer]
  needed_secrets: tuple[str, ...] = ()
  optional_secrets: tuple[str, ...] = ()

  def __post_init__(self) -> None:
    validate_segment('namespace', self.namespace)

  @staticmethod
  def of(namespace: str, server_cls: type[MCPServer], *args: Any, **kwargs: Any) -> MCPServerSpec:
    """spec for a server class that declares its secrets as class attributes.

    the escape hatch for irregularly-shaped servers; roster-based servers
    (a module-level list of tool functions) declare a `Toolset` instead.
    """
    return MCPServerSpec(
      namespace=namespace,
      build=functools.partial(server_cls, *args, **kwargs),
      needed_secrets=tuple(server_cls.needed_secrets),
      optional_secrets=tuple(server_cls.optional_secrets),
    )


class _AnyCommand:
  def __repr__(self) -> str:
    return 'ANY'


ANY = _AnyCommand()
BrashCommand = str | _AnyCommand

# the namespace every `cli(...)` tool serves under
CLI_NAMESPACE = 'cli'


@dataclass(frozen=True)
class ReachKey:
  """what one reach entry decides: its kind, and the name it decides under that
  kind. A persona's entries resolve per key along the MRO."""

  kind: str
  name: str

  def __str__(self) -> str:
    return f'{self.kind} {self.name!r}'


class ReachEntry:
  """one harness-neutral reach entry under its key."""

  @property
  def key(self) -> ReachKey:
    raise NotImplementedError

  def merge(self, other: ReachEntry) -> Optional[ReachEntry]:
    """this entry and `other`, declared under one key by one class, reduced to
    one entry; None where the pair is refused."""
    return self if other == self else None


@dataclass(frozen=True)
class Files(ReachEntry):
  write: bool = True

  @property
  def key(self) -> ReachKey:
    return ReachKey('group', 'files')

  def merge(self, other: ReachEntry) -> Optional[ReachEntry]:
    if isinstance(other, Files):
      return self if self.write else other
    return None

  def __str__(self) -> str:
    return 'files' if self.write else 'files(write=False)'


@dataclass(frozen=True)
class Brash(ReachEntry):
  """the command list reachable through the harness shell: finite `commands`,
  or any command where `unrestricted`."""

  commands: tuple[str, ...] = ()
  unrestricted: bool = False

  def __post_init__(self) -> None:
    if self.unrestricted == (len(self.commands) > 0):
      raise ValueError('a brash reach is either unrestricted or a finite command list')

  @property
  def key(self) -> ReachKey:
    return ReachKey('group', 'brash')

  def merge(self, other: ReachEntry) -> Optional[ReachEntry]:
    if not isinstance(other, Brash):
      return None
    if self.unrestricted or other.unrestricted:
      return Brash(unrestricted=True)
    return Brash(commands=tuple(dict.fromkeys(self.commands + other.commands)))

  def __str__(self) -> str:
    if self.unrestricted:
      return 'brash(ANY)'
    return f'brash({", ".join(repr(command) for command in self.commands)})'


@dataclass(frozen=True)
class Web(ReachEntry):
  @property
  def key(self) -> ReachKey:
    return ReachKey('group', 'web')

  def __str__(self) -> str:
    return 'web'


@dataclass(frozen=True)
class Delegation(ReachEntry):
  @property
  def key(self) -> ReachKey:
    return ReachKey('group', 'delegation')

  def __str__(self) -> str:
    return 'delegation'


@dataclass(frozen=True)
class Mount(ReachEntry):
  toolset: Toolset[Any]
  tool_names: tuple[str, ...]

  @property
  def key(self) -> ReachKey:
    return ReachKey('mount', self.toolset.namespace)

  @property
  def spec(self) -> MCPServerSpec:
    return self.toolset.manifest(*self.tool_names)

  def merge(self, other: ReachEntry) -> Optional[ReachEntry]:
    if isinstance(other, Mount) and other.toolset is self.toolset:
      return Mount(self.toolset, tuple(dict.fromkeys(self.tool_names + other.tool_names)))
    return None

  def __str__(self) -> str:
    return f'mount({self.toolset.namespace}: {", ".join(self.tool_names)})'


@dataclass(frozen=True)
class Cli(ReachEntry):
  words: tuple[str, ...]
  argument_names: tuple[str, ...]

  @property
  def name(self) -> str:
    return '_'.join(self.words).replace('.', '_')

  @property
  def key(self) -> ReachKey:
    return ReachKey('cli', self.name)

  @property
  def spec(self) -> MCPServerSpec:
    words, argument_names, name = self.words, self.argument_names, self.name

    def build() -> MCPServer:
      from bro.llm import cli_tool

      return cli_tool.build_server(words, argument_names, name)

    return MCPServerSpec(namespace=CLI_NAMESPACE, build=build)

  def __str__(self) -> str:
    return f'cli({", ".join(repr(word) for word in (" ".join(self.words), *self.argument_names))})'


@dataclass(frozen=True)
class Server(ReachEntry):
  spec: MCPServerSpec

  @property
  def key(self) -> ReachKey:
    return ReachKey('server', self.spec.namespace)

  def __str__(self) -> str:
    return f'server({self.spec.namespace})'


@dataclass(frozen=True)
class Source(ReachEntry):
  source: DataSource

  @property
  def key(self) -> ReachKey:
    return ReachKey('source', self.source.namespace)

  def __str__(self) -> str:
    return f'source({self.source.name})'


@dataclass(frozen=True)
class Man(ReachEntry):
  page: FileSource

  @property
  def key(self) -> ReachKey:
    return ReachKey('man', self.page.name)

  def __str__(self) -> str:
    return f'man({self.page.name!r})'


@dataclass(frozen=True)
class Revoked(ReachEntry):
  withheld: ReachKey

  @property
  def key(self) -> ReachKey:
    return self.withheld

  def __str__(self) -> str:
    return f'revoke({self.withheld})'


@dataclass(frozen=True)
class ToolLayer:
  """one composable declaration: raw server specs, each keyed by the namespace
  it serves, and reach entries, each under its own key."""

  server_specs: tuple[MCPServerSpec, ...] = ()
  entries: tuple[ReachEntry, ...] = ()

  def __post_init__(self) -> None:
    if not isinstance(self.server_specs, tuple) or any(
      not isinstance(spec, MCPServerSpec) for spec in self.server_specs
    ):
      raise TypeError('server_specs must be a tuple of MCPServerSpec values')
    if not isinstance(self.entries, tuple) or any(
      not isinstance(entry, ReachEntry) for entry in self.entries
    ):
      raise TypeError('entries must be a tuple of ReachEntry values')
    if len(self.server_specs) == 0 and len(self.entries) == 0:
      raise ValueError('a tool layer must declare a server or a reach entry')

  @property
  def reach_entries(self) -> tuple[ReachEntry, ...]:
    """every entry this layer contributes, in declaration order."""
    return (*(Server(spec) for spec in self.server_specs), *self.entries)

  def __or__(self, other: ToolLayer) -> ToolLayer:
    """both layers' declarations as one layer."""
    return ToolLayer(
      server_specs=self.server_specs + other.server_specs,
      entries=self.entries + other.entries,
    )


@dataclass(frozen=True)
class Reach:
  """what a persona's `tools` fold to, before any harness serves it: the groups
  it declares, and the servers and data sources it mounts."""

  files: Optional[Files] = None
  brash: Optional[Brash] = None
  web: Optional[Web] = None
  delegation: Optional[Delegation] = None
  server_specs: tuple[MCPServerSpec, ...] = ()
  sources: tuple[DataSource, ...] = ()

  @property
  def groups(self) -> tuple[Files | Brash | Web | Delegation, ...]:
    """the declared groups, in the order files, brash, web, delegation."""
    return tuple(
      group for group in (self.files, self.brash, self.web, self.delegation) if group is not None
    )


def files(*, write: bool = True) -> ToolLayer:
  """reach the workspace's files: read, search, and, where `write`, modify them."""
  return ToolLayer(entries=(Files(write=write),))


def web() -> ToolLayer:
  """fetch and search web content."""
  return ToolLayer(entries=(Web(),))


def delegation() -> ToolLayer:
  """start work in the harness's own agents, outside the framework's summons."""
  return ToolLayer(entries=(Delegation(),))


def brash(*commands: BrashCommand) -> ToolLayer:
  """declare the command list reachable through the harness shell.

  A finite list uses brash word patterns: an unquoted `*` matches within one
  argument and a final unquoted `...` admits further arguments. `ANY` lifts the
  command list and must be the declaration's only argument.
  """
  if len(commands) == 0:
    raise ValueError('brash needs at least one command or ANY')
  if ANY in commands:
    if commands != (ANY,):
      raise ValueError('ANY must be the only argument of its brash declaration')
    return ToolLayer(entries=(Brash(unrestricted=True),))
  from bro.brash import validate_entries

  return ToolLayer(entries=(Brash(commands=validate_entries(cast(tuple[str, ...], commands))),))


def mount(toolset: Toolset[Any], *tool_names: str) -> ToolLayer:
  if not isinstance(toolset, Toolset):
    raise TypeError('mount requires a Toolset')
  return ToolLayer(entries=(Mount(toolset, toolset.resolve(tool_names)),))


_COMMAND_WORD = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')


def cli(command: str, *argument_names: str) -> ToolLayer:
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
    raise ValueError('cli needs a command')
  for word in words:
    if _COMMAND_WORD.fullmatch(word) is None:
      raise ValueError(
        f'{word!r} is not a command word; a declaration names a program and its '
        'subcommands, and nothing a shell would interpret'
      )
  entry = Cli(words, argument_names)
  validate_segment('tool name', entry.name)
  return ToolLayer(entries=(entry,))


def source(data_source: DataSource) -> ToolLayer:
  """mount a read-only data source; its summary joins the composed prompt."""
  from bro.datasources.base import DataSource

  if not isinstance(data_source, DataSource):
    raise TypeError('source requires a DataSource')
  return ToolLayer(entries=(Source(data_source),))


def man(topic: str) -> ToolLayer:
  """declare the reference page `topic` into the bro's one manual. An unknown
  topic raises here, at declaration."""
  from bro.datasources.references import page

  return ToolLayer(entries=(Man(page(topic)),))


def revoke(*layers: ToolLayer) -> ToolLayer:
  """withhold the keys of the entries `layers` declare, whatever their level or
  subset; a descendant may grant them again."""
  keys = tuple(dict.fromkeys(entry.key for layer in layers for entry in layer.reach_entries))
  if len(keys) == 0:
    raise ValueError('revoke needs at least one entry to withhold')
  return ToolLayer(entries=tuple(Revoked(key) for key in keys))


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
  optional_secrets declares credentials the tools use when present but do not require.
  """

  # credentials the toolset's tools read through the store when the set is
  # independent of the tool subset; the `get_secrets` default returns it.
  secrets: ClassVar[tuple[str, ...]] = ()
  # credentials used when present but not required for the toolset to run.
  optional_secrets: ClassVar[tuple[str, ...]] = ()

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

  def resolve(self, tool_names: tuple[str, ...]) -> tuple[str, ...]:
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

    names = self.resolve(tool_names)
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
    server.optional_secrets = self.optional_secrets
    server.tool_universe = tuple(self._by_name)
    return server

  def manifest(self, *tool_names: str) -> MCPServerSpec:
    """the spec of a server scoped to `tool_names`, all of them for none."""
    names = self.resolve(tool_names)
    return MCPServerSpec(
      namespace=self.namespace,
      build=lambda: self.build(*names),
      needed_secrets=self.get_secrets(names),
      optional_secrets=self.optional_secrets,
    )
