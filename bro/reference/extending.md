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
- `bro.harnesses` — one installed driving-harness object per entry, `<name> = "<module>:<object>"`;
  each object derives from `bro.harness.Harness`, its `name` matches the entry, and a managed harness also implements `ride.harness.SessionHarness`
- `bro.trail_formats` — trail-format objects, `<name> = "<module>:<object>"`;
  each object is a `bro.trails.backends.TrailFormat` whose `name` matches the entry

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
- `tools = [...]` is the bro's whole tool reach, one list imported from `bro.mcp`:
  nothing reaches the bro until an entry declares it.
  - `files()` reaches the workspace's files, reading, searching, and modifying them, and `files(write=False)` only reads and searches them.
  - `brash('git log ...', 'gh pr view *')` declares the command list the persona may run through the harness shell, and `brash(ANY)` an unrestricted shell (below).
  - `web()` fetches and searches web content.
  - `delegation()` starts work in the harness's own agents, outside the framework's summons and so outside their isolation, credential scoping, and recording.
  - `mount(project_tools.mcp.toolset)` adds a contributing package's full toolset ("Adding a toolset" below), and `mount(project_tools.mcp.toolset, 'search', 'update')` scopes the mount to specific tools, validated at declaration.
  - `cli('bro list')` serves one installed CLI command as a generated tool (below).
  - `source(YourSource())` mounts a read-only data connector, whose summary joins the composed prompt ("Adding a data source" below).
  - `man('conditions')` declares one reference page into the bro's single `man` source;
    every page the class hierarchy declares folds into it.
  - `ToolLayer(server_specs=(spec,))` mounts a raw `MCPServerSpec`.
  - `revoke(delegation())`, `revoke(mount(brog_mcp.toolset))`, or `revoke(cli('bro list'))` withholds what the wrapped entries declare, whatever their level or subset.

  `files`, `brash`, `web`, and `delegation` are the tool groups.
  They are harness-neutral:
  each harness serves a group with its own tools, and `bro show` marks a group a harness leaves unserved (`bro/reference/ride.md`, "Bro harness" and "The claude argv").
  Without `files`, Claude still serves a `Read` held to the session's own output, and a session refuses to start where the repository's Claude configuration could take that `Read` around its gate, as a finite command list does for its shell (below).
- `brash('git log ...', 'gh pr view *')` declares a finite command list.
  An entry is written like a command and split with shell quoting:
  each word matches one argument, an unquoted `*` matches any text within its argument, and a final unquoted `...` admits any further arguments, including none.
  Quoting makes either marker literal, so `grep -E 'a*b' ...` admits only the literal pattern `a*b`.
  Leading `NAME=value` words are part of the entry.
  An entry names one simple command with a literal program word;
  shell operators, expansions, a non-final `...`, brash builtins, and bash builtins brash does not implement are refused at declaration.
  `brash(ANY)` marks the shell unrestricted and therefore runs lines in plain bash rather than through the finite-list interpreter, and an empty declaration is invalid.
- `cli('bro list')` serves one installed CLI command as a generated tool in the `cli` namespace (`cli::bro_list`).
  The command is a program name and any subcommands;
  trailing names narrow what the tool exposes (`cli('bro show', 'name')` withholds `--system-prompt`).
  Every generated tool also takes `timeout_seconds`, after which the command is killed,
  so a command argument of that name must be withheld.
  Nothing is read at declaration:
  the signature is derived at build from the command's own argument declarations, so a command that cannot be read
  — not an installed CLI, a dispatcher rather than a leaf, an argument shape that cannot be described
  — fails there.
  Credentials the command reads are the declaring bro's `extra_secrets`.
- Every `tools` entry has a key, its kind with a name, so entries of different kinds never share one:
  a group's name, a mounted toolset's namespace, a data source's namespace, a reference page's topic, a `cli` entry's generated tool name, and the namespace a raw spec serves.
  A layer carrying several entries, a raw `ToolLayer` or a `|` of layers, contributes each under its own key.
- `tools` resolve per key along the MRO.
  Conditions select first, so an entry whose `when` or `iff` condition does not hold is omitted before keys resolve.
  Then the first class in the MRO that declares a key decides it, and that class's entries for the key reduce by kind:
  files levels to the wider, command lists to their union with `ANY` absorbing them, mounts of one toolset to the union of their tool subsets, and identical entries to one.
  Any other pair under one key fails construction rather than letting list order decide:
  a `revoke` beside a grant, two different sources or raw specs, or two `cli` entries for one tool exposing different arguments.
  A subclass therefore declares only what it adds, narrows, or withholds:
  `files(write=False)` under an inherited `files()` narrows it, and a shorter `brash(...)` list narrows an inherited one;
  an unmet `when(feature('x'), files(write=False))` leaves the base's `files()` in force;
  and a descendant may grant again what an ancestor revoked.
- An entry may be gated on the bro's `#creds` and `#features` with `when(...)` (`from bro.base.condition import when`;
  `when` also accepts a plain bool for a genuinely static predicate), or choose among alternatives with `iff(c1, a1, c2, a2[, e])`, which raises when nothing matches and no else item is given.
  A declaration cannot condition on the harness:
  every harness serves the one reach a bro's entries fold to.
  See `bro/reference/conditions.md`.
  Declarations skip the `ClassVar` annotation, since BaseBro's class-level declarations carry the types;
  a ruff configuration selecting RUF012 ignores it for persona modules.
- `llm_spec = openai.LLMSpec(...)` (or any other bro-native provider's `LLMSpec`) overrides the recipe the bro-native engine runs the bro under;
  a Claude Code session runs under the recipe its own launch names.
  Per-instance overrides go through `YourBro.create(spec)`.
- `extra_secrets = ('github',)` declares credentials no component expresses (a bro's environment needs).
  MRO-walked and unioned;
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
- `may_launch = ('webview',)` seeds one whole `:launch.<type>` key per worker type before the launch's configuration layers fold.
  Fields, flags, and pass rights remain host- or launch-configured, and any layer may revoke the seeded key.
  The declaration is MRO-walked and unioned like `may_summon`;
  `bro` is refused because the framework already seeds it, and an uninstalled type fails when the host computes a launch for the persona.
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
  spell and parameter names must fit the wire charset, and malformed declarations fail at load;
  an entry naming no file, one escaping the directory, or two entries sharing a stem fails the bro's construction.
  Spell names are imperative verb phrases (`fix`, `land`, `run-pr`, `orchestrate`), kebab-cased when multi-word.
  Prose that refers to *running* one
  — in a spell, a prompt, or a doc
  — marks it `[[…]]`, hyphens as spaces and the phrasing fitted to the sentence (`hand off to [[run pr]]`, `blocks [[land]] later`);
  canonical `spell::<name>` stays for the mechanism and for component inventories.
  Spells are MRO-walked:
  each ancestor's declaration contributes, derived classes override parents on name collision, and the concrete `Bro`'s spells reach every bro deriving from it.
  The full description is the tool description;
  keep it useful for tool selection rather than optimizing its first sentence.

### Brash command-list policy

A finite `brash(...)` command list states what a persona is meant to run and catches a model that goes off-script, consistently across harnesses.
It is policy, not a containment boundary:
a general-purpose listed program such as `git -c`, `uv run`, `find -exec`, `xargs`, a shell, or an interpreter can run other programs, and brash runs on the host.
Use a `cli(...)` tool for a command that must be held to one fixed argument shape, and use sandboxing for containment.

Under a finite command list, brash runs the lines the model starts:
through `Bash` and `Monitor` on Claude, whose calls a gate rewrites into brash calls, and through its jobs and watches on the bro harness (`bro/reference/ride.md`, "The claude argv" and "Bro harness").
`brash(ANY)` runs them in bash instead.
A `cd` inside a line moves brash for the rest of that line only, so on Claude it no longer carries to the next call.
What the session's Claude configuration runs is not checked:
hooks from your, the repository's, or your organization's managed settings, plugins, and a skill's inline `!cmd` lines, which Claude runs without consulting any `PreToolUse` hook.
So a model that can write files can get around its command list:
it writes a skill whose `!cmd` runs what it likes, and the skill takes effect from the next session.
Claude's own guard against that is a permission prompt, which ride skips under every hold but `guided`;
even Claude's auto mode let a benign-looking skill write through when probed.
The command list catches a model that goes off-script, not one working to get around it.
A session with a finite command list refuses to start while its project or local settings, the skills, commands, and agents Claude would load, or its enabled plugins carry a `PreToolUse` hook matching `Bash` or `Monitor`,
which could rewrite their calls after the gate.
Managed settings are your organization's policy, which outranks a persona's:
a managed policy that allows only managed hooks turns the gate off by design, while a managed hook's own rewrite runs before the gate, which runs what it wrote in brash.

Brash runs the declared subset of bash syntax itself and checks every literal program before anything starts, then checks each expanded argv as its command starts.
Its command language includes pipelines, boolean and sequential lists, background commands, negation, subshells and groups, `if`, `for … in`, `while`, `until`, `case`, comments, assignments, redirects, here-documents, and here-strings.
Its word forms include command and process substitution, quoting, variables and special parameters, `$'…'`, tilde expansion, splitting, globs, and brace patterns.
It implements `cd`, `pwd`, `echo`, `exit`, `true`, `false`, `:`, and a plain `wait` as builtins;
other commands need a list entry, including programs commonly shadowed by bash builtins such as `test`, `[`, `printf`, and `kill`.
Unsupported syntax and bash builtins are refused rather than passed to a host shell.

A plain assignment and a `for` variable may set only a brash-local name, not a name inherited in the environment;
prefix assignments on a command are matched as part of its entry and reach that command's environment.
Input redirects may read any path into an admitted command.
Output redirects may write files only when the persona's file reach is writable;
here-documents, here-strings, descriptor duplication and closure, and `/dev/null`, `/dev/stdout`, and `/dev/stderr` are always available.


## Registering a bro

Register the class under the distribution's `bro` entry-point group, `your-name = "your.module:YourBro"`, and sync ("Entry-point groups" above).
There is no auto-discovery:
the entry is what makes `create_bro('your-name')` resolve wherever the distribution is installed, and its key must equal the class's `name`, which the registry validates when it lazily imports the module.
Two installed distributions claiming one name fail the registry rather than letting import order decide.
Package-relative spells and MRO-resolved `tools` declarations work across distributions.
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
Bind it to a bro by declaring `source(YourSource())` in the bro's `tools`.

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
- `optional_secrets` is the credential set the tools use when present but can run without;
  it becomes the mounted server's `optional_secrets` and enters the bro's best-effort credential tier.
- `state` builds the per-server object, once per built server in the serving process and never on a metadata surface, and every tool declaring a `Context`-annotated parameter receives it as `context.state`;
  `close` releases it when the run's lifetime ends (`BaseBro.close()`).
  A toolset without state leaves both out and declares `Toolset[None]`.
- A description may carry `{{…}}` directives over `#tools`, the build's selected roster (`bro/reference/conditions.md`, "Server-domain vocabularies").
- `mount(toolset)` and `mount(toolset, 'lookup')` are the declaration-side entries ("Declaring a bro" above);
  a `bro.toolsets` entry named after the namespace (`catalog = "acme.catalog:toolset"`) additionally lets `mcp-server catalog` serve the toolset standalone.
- `MCPServerSpec.of('<namespace>', ServerClass, *ctor_args)` is the manifest escape hatch for a server class of another shape, naming the namespace the built server serves;
  wrap it in `ToolLayer(server_specs=(spec,))` to mount it.
