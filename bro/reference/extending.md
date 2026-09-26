# Extending the framework

How an installed distribution adds to the framework:
the entry-point groups it contributes through, and how a bro, a data source, and a toolset are declared and registered.
Declarations compose through the vocabulary of `bro.mcp`;
conditions on them follow `conditions.md`, and the text they carry follows `template.md`.

## Entry-point groups

A distribution contributes through these `[project.entry-points]` groups of its `pyproject.toml`:

- `bro` — personas, `<name> = "<module>:<ClassName>"` ("Registering a bro" below)
- `bro.toolsets` — standalone MCP toolsets, each entry named after its namespace and targeting its module's `toolset` object ("Adding a toolset" below)
- `bro.mcp.targets` — assembled target prefixes;
  each resolver accepts the value after `<prefix>:` and returns live MCP servers
- `bro.credentials` — credential-registry entries
- `bro.credential_sources` — minting source types
- `bro.brog.backends` — task-tracker backends
- `bro.session_commands` — console scripts exposed on a managed session's PATH
- `bro.worker_types` — worker classes served through the common `launch` kind;
  each entry's name matches its `bro.worker_types.WorkerType.name`

Entry points are installation metadata:
sync the environment (`uv sync`) after adding or removing one, while editing an already-declared target module needs no reinstall.
Name-keyed groups load only the matching entry, while credential-registry assembly loads every `bro.credentials` contribution, so those target modules must remain import-cheap.

## Declaring a bro

A bro is a class deriving from the concrete `Bro` (`from bros.bro import Bro`), which carries the shared defaults every bro inherits;
derive from `BaseBro` only when opting out of them is the persona's point.
It lives in a module of the contributing distribution, conventionally `bros/<name>/__init__.py`
— `bros` is a shared PEP 420 namespace, so it has no `__init__.py` of its own.
Declare `name`, `description`, and `system_prompt` as class attributes, and the bro's components beside them:

- `system_prompt = "..."` — class-level.
  When you `class B(A)` and both declare `system_prompt`, `__init__` concatenates A's then B's (MRO base-to-derived) so subclasses only declare their *additions*.
- `data_sources = [YourSource()]` for read-only data connectors
- `data_sources = [man('conditions'), man('ride')]` (`from bro.datasources.references import man`) declares reference pages one topic at a time;
  every page the class hierarchy declares folds into the bro's single `man` source
- `tools = [mount(project_tools.mcp.toolset)]` adds a contributing package's full toolset ("Adding a toolset" below)
- `tools = [mount(project_tools.mcp.toolset, 'search', 'update')]` scopes the mount to specific tools (validated at declaration)
- `tools = [when(harness == 'bro', mount(dev_mcp.toolset))]` (`from bros.dev import mcp as dev_mcp`, supplied by `bro-dev`) mounts its file and search tools only on the bro harness
- `tools = [claude.block(*claude.SHELL), shell('git status', 'git diff')]` declares the exact command lines the persona may run on either harness;
  the block lets the finite roster narrow Claude's shell, while `shell(ANY)` leaves its unblocked shell unrestricted and an empty declaration is invalid
- `tools = [cli('bro list')]` serves one installed CLI command as a generated tool in the `cli` namespace (`cli::bro_list`).
  The command is a program name and any subcommands;
  trailing names narrow what the tool exposes (`cli('bro show', 'name')` withholds `--system-prompt`).
  Every generated tool also takes `output_offset` / `output_limit`, a window over the command's output, and `timeout_seconds`, after which the command is killed,
  so a command argument of any of these names must be withheld.
  Nothing is read at declaration:
  the signature is derived at build from the command's own argument declarations, so a command that cannot be read
  — not an installed CLI, a dispatcher rather than a leaf, an argument shape that cannot be described
  — fails there.
  Credentials the command reads are the declaring bro's `extra_secrets`.
- `tools = [when(harness == 'claude', block('Read', 'Write'))]` removes harness-native tools.
  One block may group several related names;
  it must be gated away from `harness == 'bro'`, whose native loop exposes only the declared tools, or construction raises.
  `tools = [when(harness == 'claude', allow_commands('Monitor', 'journalctl -f'))]` hands one of those tools back narrowed to the commands it names, and `serve('TaskStop')` hands one back whole where there is no command line to narrow on;
  either way the tool must be blocked too, since handing back bounds nothing a bro does not otherwise withhold.
  Import `block`, `allow_commands`, `serve`, `harness`, and `mount` from `bro.mcp`.
- `tools` and `data_sources` are walked along the MRO and concatenated, so a `ReviewDev(Dev)` subclass declares only its additional components and retains Dev's declarations.
- An entry in either list may be gated on surface facts with `when(...)` (`from bro.base.condition import when`;
  `when` also accepts a plain bool for a genuinely static predicate), or choose among alternatives with `iff(c1, a1, c2, a2[, e])`, which raises when nothing matches and no else item is given.
  Conditions evaluate at assembly, so an unmatched declaration is never applied
  — see `bro/reference/conditions.md`.
  Declarations skip the `ClassVar` annotation, since BaseBro's class-level declarations carry the types;
  a ruff configuration selecting RUF012 ignores it for persona modules.
- `llm_spec = openai.LLMSpec(...)` (or any other bro-native provider's `LLMSpec`) overrides the recipe the bro-native engine runs the bro under;
  a Claude Code session runs under the recipe its own launch names.
  Per-instance overrides go through `YourBro.create(spec)`.
- `extra_secrets = ('github',)` declares credentials no component expresses (a bro's environment needs).
  MRO-walked and unioned like `tools`;
  folded into `bro.needed_secrets()`, which the host hydrates into the scoped container store.
  Most secrets come from the declared MCP servers / data sources / `llm_spec` and need no entry here
  — see `bro/AGENTS.md`, "Credential manifest".
- `features = {'brog': creds.contains('brog')}` declares named optional capabilities
  — feature name → the gate deciding whether it's on:
  a `Condition` over the environment's resolvable credentials (`from bro.mcp import creds`), or a plain bool (`True` pins it on, `False` disables it; MRO-merged, derived wins per name, and `False` is terminal — descendants cannot re-enable).
  Gate components with `when(feature('brog'), …)` (`from bro.bro import feature`) and text with `{{iff #features contains brog}}`;
  a gated component's secrets enter the manifest only where its gates resolve, and the gate's own credential is tiered with the feature.
  See `bro/reference/conditions.md` "Bro features".
- `may_summon = ('reviewer',)` seeds members of `launch.bro.bros`, adjusted per launch or summon request by `--grant @bro` and `--revoke @bro`.
  A summon request may grant the child only launch authority its summoner holds;
  the child's own seed and configuration are independent, and the host's depth cap separately bounds the launch tree.
  The declaration is MRO-walked and unioned like `extra_secrets`;
  each seed expands transitive launch authority and should be added deliberately.
  The complete permission-document fold is `bro/reference/ride.md`, "Session permissions and credentials".
- `provisioning = (provision_hooks,)` declares session-start steps for the session's workspace
  — each is a `Callable[[Path], None]` applied to the workspace root by whatever starts the session (`do-ride` for a managed one, on either harness).
  MRO-walked and concatenated like `extra_secrets`.
  Every session start runs them, resumes included, so each step is idempotent.
- `spells = ('fix.md', 'run-pr.md')` declares spells:
  each entry is a markdown file's path relative to the `spells/` directory beside the declaring module.
  The file opens with flat frontmatter
  — `name`, `description`, an optional one-line JSON `parameters` map, and an optional informational `version` bumped when the spell changes
  — and carries the markdown body after the closing `---`.
  A parameter is a string, and a `?` suffix on its key marks it optional (`parameters: {"task": "task ref", "notes?": "optional context"}`).
  A frontmatter value is either inline after the key or, where a bare `key:` is followed by a blank line, the block of lines under it up to the next blank line or the closing fence
  — folded into one paragraph on single spaces, so a long description breaks semantically in the file (one clause per line, for reviewable diffs) and still reaches the tool as one paragraph.
  The filename stem is the spell's name, canonical and validated against `name:`;
  spell and parameter names must fit the wire charset, parameter name `offset` is reserved, and malformed declarations fail at load;
  an entry naming no file, one escaping the directory, or two entries sharing a stem fails the bro's construction.
  Spell names are imperative verb phrases (`fix`, `land`, `run-pr`, `orchestrate`), kebab-cased when multi-word.
  Prose that refers to *running* one
  — in a spell, a prompt, or a doc
  — marks it `[[…]]`, hyphens as spaces and the phrasing fitted to the sentence (`hand off to [[run pr]]`, `blocks [[land]] later`);
  canonical `spell::<name>` stays for the mechanism and for component inventories.
  Spells follow the same MRO walk as `system_prompt` and `tools`:
  each ancestor's declaration contributes, derived classes override parents on name collision, and the concrete `Bro`'s spells reach every bro deriving from it.
  The full description is the tool description;
  keep it useful for tool selection rather than optimizing its first sentence.

## Registering a bro

Register the class under the distribution's `bro` entry-point group, `your-name = "your.module:YourBro"`, and sync ("Entry-point groups" above).
There is no auto-discovery:
the entry is what makes `create_bro('your-name')` resolve wherever the distribution is installed, and its key must equal the class's `name`, which the registry validates when it lazily imports the module.
Two installed distributions claiming one name fail the registry rather than letting import order decide.
Package-relative spells and MRO-collected `tools` / `data_sources` declarations work across distributions.
Project launch defaults (`[tool.bro] default`, image repository) are documented in `bro/reference/ride.md`, "Per-project defaults".

## Adding a data source

Set `name` (slug) and `summary` (one-line; injected into the system prompt of every Bro that uses it).
For the common search/fetch shape, subclass `SearchableDataSource` and implement `async search(query, limit) -> list[Hit]` and `async _fetch_content(id) -> str` (the raw record).
The base provides `fetch(id, query=None)`:
it returns the raw record when no query is given and otherwise summarises it for the query via `mu`
— so you don't write summarisation per source.
That summary path depends on the `openai` key, declared once on the base as an `optional_secret`;
with the key absent a non-null `query` raises (no raw-text fallback).
For other shapes (e.g. a singleton fact like `current_time.py`), subclass `DataSource` directly and override `as_mcp_server()` to expose whatever tools fit, stamping `self.namespace` onto the returned server.
When an upstream HTTP/network failure makes the source temporarily unusable, raise `bro.datasources.base.SourceUnavailable(source, reason)` rather than letting raw transport exceptions escape
— the agent loop turns it into a tool result the model can route around.
If the source reads a credential through the store, declare it with `needed_secrets = ('catalog',)` (or `optional_secrets` for one it degrades without),
so the host hydrates it into any bro that uses the source (`bro/AGENTS.md`, "Credential manifest").
Bind to a Bro by declaring `data_sources = [YourSource()]` on its class.

## Adding a toolset

A toolset is an ordinary module holding one `bro.mcp.Toolset` per namespace, conventionally named `toolset` and defined above the functions that register on it:

```python
from bro.llm.mcp import Context
from bro.mcp import Toolset


class _Toolset(Toolset[Catalog]):
  secrets = ('catalog',)


toolset = _Toolset('catalog', state=Catalog.connect, close=Catalog.close)


@toolset.tool('look a product up by its id')
def lookup(product_id: str, context: Context[Catalog]) -> str:
  return context.state.describe(product_id)
```

- `@toolset.tool('<description>')` registers the function under its own name, so this tool is `catalog::lookup`;
  the signature is the tool's schema, the description its text, and the decorator returns the function unchanged.
  A tool returns `str`, or a JSON-ready `dict` for structured output.
  Neither the namespace nor a tool name may contain `__`, the wire separator between them.
- `secrets` is the credential set the tools read through the store, and becomes the mounted server's `needed_secrets`;
  override `get_secrets(tool_names)` when the set depends on the selected subset.
- `state` builds the per-server object, once per built server in the serving process and never on a metadata surface, and every tool declaring a `Context`-annotated parameter receives it as `context.state`;
  `close` releases it when the run's lifetime ends (`BaseBro.close()`).
  A toolset without state leaves both out and declares `Toolset[None]`.
- A description may carry `{{…}}` directives over `#tools`, the build's selected roster (`bro/reference/conditions.md`, "Server-domain vocabularies").
- `mount(toolset)` and `mount(toolset, 'lookup')` are the declaration-side entries ("Declaring a bro" above);
  a `bro.toolsets` entry named after the namespace (`catalog = "acme.catalog:toolset"`) additionally lets `mcp-server catalog` serve the toolset standalone.
- `MCPServerSpec.of(ServerClass, *ctor_args)` is the manifest escape hatch for a server class of another shape;
  wrap it in `ToolLayer(server_specs=(spec,))` to mount it.
