# Extending the framework

How an installed distribution adds to the framework:
the entry-point groups it contributes through, and how a bro, a data source, and a toolset are declared and registered.
Declarations compose through the vocabulary of `bro.mcp`;
conditions on them follow `conditions.md`, and the text they carry follows `template.md`.

## Entry-point groups

A distribution contributes through these `[project.entry-points]` groups of its `pyproject.toml`:

- `bro.bros.<home>` — personas belonging to `<home>`, `<name> = "<module>:<ClassName>"` ("Registering a bro" below)
- `bro.settings` — home settings, `<home> = "<package>"`, read as that package's `bro-settings.toml` resource ("Home settings" below)
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
- `bro.file_types` — file-type declarations, `<name> = "<module>:<object>"`;
  each object is a `bro.files.FileType` whose `name` matches the entry ("Adding a file type" below)
- `bro.llm.providers` — provider declaration modules, `<name> = "bro.<name>.declaration"`;
  the recipe's `TYPE` matches the entry name (`[a-z][a-z0-9-]*`), and the module exports `Recipe`, failure signatures and pricing.
  A provider contributes its credential kind separately through `bro.credentials` from an import-free literal module.
  Provider package hubs import nothing, and declarations import no SDK at module level and no harness.
  Explicit selection loads only that declaration; searches warn and skip a broken declaration.
  Provider and model adoption follows [llm-providers](llm_providers.md).
- `bro.llm.wires` — SDK-free wire decode modules, `<name> = "<module>"`;
  a module exports matching `NAME`, `CHAINING`, `decode(response)` and `counts(usage)`, and may claim untagged records with `LEGACY = True`
- `bro.llm.wire_clients` — client classes implementing `bro.llm.wire.Wire`, `<name> = "<module>:<Client>"`;
  the name matches the class's `NAME` and its decode-side entry, and the wire distribution provides its SDK dependencies in the `client` extra.
  The native harness serves recipes whose `WIRE` has an installed client, and auxiliary calls open the same clients through `bro.llm.wires.open`
- `bro.trail_formats` — trail-format objects, `<name> = "<module>:<object>"`;
  each object is a `bro.trails.backends.TrailFormat` whose `name` matches the entry

Entry points are installation metadata:
sync the environment (`uv sync`) after adding or removing one, while editing an already-declared target module needs no reinstall.
Name-keyed groups load only the matching entry, except file-type identification (`bro.files.registry.identify`), which loads every installed type to match content.
Credential-registry assembly also loads every `bro.credentials` contribution.
These target modules must remain import-cheap.

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
  - `man(conditions)` declares one reference page, a `FileSource` such as `bro.datasources.references.conditions`, into the bro's single `man` source under its name as the topic;
    every page the class hierarchy declares folds into it, from whichever module declares it.
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
- Model recipes live in configuration, not persona declarations.
  The native launcher passes the resolved run recipe explicitly to the runner.
- `extra_secrets = ('github',)` declares credentials no component expresses (a bro's environment needs).
  MRO-walked and unioned;
  folded into `bro.needed_secrets()`, which the host hydrates into the scoped container store.
  Most secrets come from the declared MCP servers / data sources and need no entry here
  — see `bro/AGENTS.md`, "Credential manifest".
- `features = {'brog': creds.contains('brog')}` declares named optional capabilities
  — feature name → the gate deciding whether it's on:
  a `Condition` over the environment's resolvable credentials (`from bro.mcp import creds`), or a plain bool (`True` pins it on, `False` disables it; MRO-merged, derived wins per name, and `False` is terminal — descendants cannot re-enable).
  Gate components with `when(feature('brog'), …)` (`from bro.bro import feature`) and text with `{{iff #features contains brog}}`;
  a gated component's secrets enter the manifest only where its gates resolve, and the gate's own credential is tiered with the feature.
  See `bro/reference/conditions.md` "Bro features".
- `may_summon = ('reviewer',)` seeds `@reviewer` with full use and give before configuration layers apply.
  The child's base is independent of its starter; request gifts and caps add or restrict authority under the use/give checks.
  The declaration is MRO-walked and unioned like `extra_secrets`.
  The host's depth cap separately bounds the tree.
- `may_launch = ('webview',)` seeds the type's start permission with full use and give.
  The declaration is MRO-walked and unioned like `may_summon`.
  Guarded option values, such as profiles, VNC, or downloads, need their own grants.
  `bro` is refused because the framework already seeds it; an uninstalled type fails an applicable launch.
  The [permission reference](permissions.md) defines type options, configuration layers, and the checks.
- `provisioning = (provision_hooks,)` declares session-start steps for the session's workspace
  — each is a `Callable[[Path], None]` applied to the workspace root by whatever starts the session (`do-ride` for a managed one, on either harness).
  MRO-walked and concatenated like `extra_secrets`.
  Every session start runs them, resumes included, so each step is idempotent.
- `spells = (fix, when(feature('brog'), open_task))` declares spells:
  each entry is a `SpellFile` (`bro.spells`) over a markdown file, or a `when` / `iff` of them gated like `tools`, so a spell absent from the environment is absent from the bro's roster.
  `SpellFile(path, requires=(...))` names, by spell name and in the same grammar, the spells it runs in its own session (two spells may name each other);
  a bro offering a spell must offer every requirement that resolves, and construction fails otherwise.
  A package that owns spells defines their values in its `spells` package (`bros.lead.spells`, `bro.dev.spells`) beside the files, and bros import them from there, so one spell is shared by importing its value.
  The file opens with flat frontmatter
  — `name`, `description`, an optional one-line JSON `parameters` map, and an optional informational `version` bumped when the spell changes
  — and carries the markdown body after the closing `---`.
  A parameter is a string, and a `?` suffix on its key marks it optional (`parameters: {"task": "task ref", "notes?": "optional context"}`).
  A frontmatter value is either inline after the key or, where a bare `key:` is followed by a blank line, the block of lines under it up to the next blank line or the closing fence
  — folded into one paragraph on single spaces, so a long description breaks semantically in the file (one clause per line, for reviewable diffs) and still reaches the tool as one paragraph.
  The filename stem is the spell's name, canonical and validated against `name:`;
  spell and parameter names must fit the wire charset, and malformed declarations fail at load;
  a missing or non-markdown file, an entry that is not a `SpellFile`, or two entries sharing a stem fails the bro's construction.
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

Register the class under the distribution's `bro.bros.<home>` entry-point group, `your-name = "your.module:YourBro"`, and sync ("Entry-point groups" above).
There is no auto-discovery:
the entry is what makes `create_bro('your-name')` resolve wherever the distribution is installed, and its key must equal the class's `name`, which the registry validates when it lazily imports the module.
The group names the bro's home; a distribution may contribute bros to several homes.
Two entries claiming one bro name, even across homes, fail the registry naming both distributions rather than letting import order decide.
Package-relative spells and MRO-resolved `tools` declarations work across distributions.
`bro show` reports the bro's home.

## Home settings

A home may ship settings without registering bros, and register bros without shipping settings.
Register one settings package for a home:

```toml
[project.entry-points."bro.settings"]
my-home = "my_project"
```

Include `my_project/bro-settings.toml` in the wheel as package data.
Its launch keys are documented in [ride](ride.md#home-settings).
A home without an entry ships no settings.
Two installed entries for the same home's settings refuse every launch, naming both distributions.
Only the installation's home settings are read.

An installation names its home in `<environment>/bro-installation-home`, at `sys.prefix`, holding a `[a-z][a-z0-9-]*` name and a final newline.
The home need not declare any bros or settings.
The environment's setup writes the marker after syncing; for example:

```bash
printf 'my-home\n' > .venv/bro-installation-home
```

A missing or malformed marker refuses a launch, naming the path and the command to write it.
Frozen runtime bundles carry the home in their manifest and write the marker into both host and container environments.
An in-process `bro run` or `bro chat` reads its live environment's marker.

## Adding a data source

Set `name` (slug) and `summary` (one-line; injected into the system prompt of every Bro that uses it).
For the common search/fetch shape, subclass `SearchableDataSource` and implement `async search(query, limit) -> list[Hit]` and `async _fetch_content(id) -> str` (the raw record).
The base provides `fetch(id, query=None)`:
it returns the raw record when no query is given and otherwise summarises it for the query via `mu`
— so you don't write summarisation per source.
That summary path uses the `summary` role of the `mu` credential, declared once on the base as an `optional_secret`;
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

## Adding a file type

Register a frozen `FileType` instance from `bro.files` in `bro.file_types`:

```toml
[project.entry-points."bro.file_types"]
report = "acme.report:TYPE"
```

The declaration module imports no parser.
It declares pydantic model classes for `details` and each view's `content`, a `Limits` instance, the shipped markdown manual's `Path`, and a `provider` string in `module:attribute` form.
Only the worker imports the provider.
The entry name and the declaration's `name` must agree and follow `[a-z][a-z0-9-]*`.
Two distributions claiming the same name are an error, not a priority order.

- `signatures` is a tuple of `Signature(marker, within)` objects:
  `marker` is nonempty bytes beginning at an offset no greater than `within`, and the whole search must fit in `bro.files.types.IDENTIFICATION_BYTES`.
  An empty tuple makes the type accessible only by explicit `type`.
- `views` is a tuple of `View(name, about, content, paged)` objects with unique names:
  `about` is a nonempty single line and `paged` says whether the view accepts page selection.
- `limits` declares `max_bytes`, `max_pages`, `max_view_characters` and `max_view_entries` as positive integers.
  `max_pages=None` describes a type without pages, which cannot declare a paged view.
  Sandbox bounds belong to the runner, not the type.
- `manual` documents every field returned by `details` and by each view's `content`, including nested models, plus the type's transformations, omissions, limits and rejections.
  `bro.files.operations.validate_manual` checks field coverage, and `read_reference(type)` performs that check when serving the manual.

Installed names are read from metadata without loading declarations, and `TypeRegistry` caches each selected type.
`identify(head)` loads every installed declaration, matches its signatures and requires exactly one match; a broken declaration fails identification by name.
An explicit `type` loads only that entry, bypasses signature matching and leaves the provider to reject unreadable bytes.
A file's extension is never consulted.
The framework validates provider replies against the declared models and returns the validated objects as dictionaries under the fixed result envelopes.
Result keys and manual coverage use model field names, not serialization aliases, including in nested models.

Providers use `bro.files.text.sanitize` on strings before returning them.
It supplies stripped text and decoded tag-character payloads with input offsets, so page-text providers can report those payloads in their notes.
ZWNJ and ZWJ are retained.
`validate_strings` refuses lone surrogates and control characters other than TAB, LF and CR throughout a JSON reply, object keys included.

Without a type installed, selection fails with an installation hint, while `read_reference()` still returns shared rules and an empty type list.
The shared rules ship in `bro/files/reference.md` and are independent of any parser distribution.

### Implementing the provider

The `provider` attribute resolves to an object implementing `bro.files.protocol.Provider`, not a factory:

- `prepare() -> str` loads the parser, native libraries and codecs, warms every required parser path with built-in data, and returns a nonempty engine version.
  It runs before confinement, once in each fresh worker.
- `info(snapshot: bytes, limits: Limits) -> dict` returns the declared `details` payload.
- `view(snapshot: bytes, view: str, pages: list[int] | None, limits: Limits) -> dict` returns the selected view's `content` payload.
  `pages` is a sorted, deduplicated, one-based selection or `None` for all pages;
  use `bro.files.pages.select_pages` to check it against the document's actual count.

Both operations receive the same immutable snapshot that core hashed, not a path.
Return only the type-specific payload: core supplies the `InfoResult` or `ViewResult` envelope.
Use strict pydantic models with `extra='forbid'` to reject unexpected fields and types.
For page text, declare notes before text so a kept-result window presents warnings before the document's content.

The provider enforces the document's page count and cumulative view character and entry caps from `limits`.
Define what counts as a character or an entry in its manual, including nested collections;
reject an oversized selection rather than silently truncating it.
Raise `bro.files.protocol.InvalidInput` for a document or selection fault, with a cause and a fix constructed from fixed phrases and counts, never document-derived text.
Other exceptions become an engine failure, with their traceback confined to the log.
Messages must stand alone: the HTTP tool client receives the message, not the exception class.

After `prepare`, the worker is single-threaded and cannot open files, load further modules or libraries, start processes or threads, create sockets, or change its resource limits.
Preload every lazy parser path, codec and built-in fixture it needs before returning from `prepare`;
perform all parsing of caller bytes in `info` or `view`, under confinement.
Unavailable protections fail closed, with no weaker mode.
Runner bounds and the required protections are defined in `bro.files.confinement`;
a provider needing a different execution environment needs a runner that supports it, not an in-process parser fallback.

### Mounting and testing

A persona opts in explicitly:

```python
from bro.files import mcp as file_mcp
from bro.mcp import mount
from bros.bro import Bro


class ReportReader(Bro):
  name = 'report-reader'
  description = 'reads report files'
  tools = [mount(file_mcp.toolset)]
```

Install the provider distribution alongside the persona.
For PDFs the distribution is `bro-filetypes`;
consumers pin it with their other framework distributions (`BOOTSTRAP.md`).
No published persona mounts the file tools by default.
The tools accept filesystem paths, not artifact references, and consult `bro.files.access.current()`;
its default `AllowAll` implementation is not a path policy.

Test declaration and model validation without importing the parser, then exercise generated fixtures through the real worker and the installed entry point.
A worker test needs a Linux kernel supporting confinement;
this repository puts those tests in its `sandbox` gate stage, and tests of an in-process provider in `unit`.
Exercise an assembled persona through both the native tool registry and HTTP `tools/call`, including non-ASCII results, kept-result paging and named failures.
