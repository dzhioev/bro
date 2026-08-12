# Declarative conditioning

How the framework gates component declarations and static text on the facts of the surface that consumes them. The model is `bro/base/condition.py`: a condition is an immutable predicate object built at declaration time — constructing one reads no facts — and evaluated later against typed variables, so a class-level declaration stays an import-time constant while its truth is decided by whatever surface holds the facts. The template directive front (`bro/reference/template.md`) lowers text conditions onto the same objects, so both fronts share one evaluator, one fail-fast semantics, and one vocabulary.

## Why

Component declarations (a bro's `tools` / `data_sources`) and static text (system prompts, script bodies, tool descriptions) are written once but consumed by different surfaces — the bro-native LLM loop, `--raw` claude sessions, cw-sessions — with different toolsets, wire-name spellings, and credentials. Conditioning derives each surface's variant from one declaration, and fails fast on a typo instead of silently deciding one way forever.

## Variables

The facts a surface supplies, typed:

- `StringVariable` — a string value; an optional closed `domain` makes comparing against a literal outside it an error

- `SetVariable` — a set for membership tests; an optional closed `universe` makes testing a name outside it an error. `members` is either a materialized set or a membership predicate — the lazy form for sets whose membership is expensive to probe; only names a condition actually tests get probed

- a plain `bool`

## Conditions

An operand is a variable reference (`var('harness')`, or a ready-made placeholder like `bro.llm.mcp.harness`) or a string literal. Two condition forms, spelled the same as their directive counterparts:

- `harness == 'bro'` — equality of two strings or two booleans (`#harness = bro` in a directive)

- `creds.contains('openai')` — membership of a string in a set variable, container first (`#creds contains openai` in a directive)

`Condition.evaluate(variables)` resolves the operands and decides the predicate. Evaluation is fail-fast — it raises `ConditionError` instead of silently deciding, on:

- an unknown variable (the error lists the known names)
- a set compared with `==`; a non-set container or a non-string element in `contains`
- a boolean compared against a string
- a literal outside a domain-closed comparand's domain
- a tested name outside a set's universe

Three declaration-time guards close off the Python operators that cannot build deferred conditions:

- `'openai' in creds` raises pointing at `contains` — the interpreter coerces `__contains__`'s result to bool, so `in` would evaluate at import time, before the facts exist (the reason membership is a method)
- `harness != 'bro'` raises — there is no negated form; compare against the intended value
- `bool(condition)` raises — a condition has no truth value until evaluated, so `if harness == 'bro':` in plain code fails at the line that wrote it instead of always taking the branch

## Declarative lists: when / iff / select

- `when(condition, item)` — optional inclusion: the item enters the list only when the condition holds; absence is meaningful, never an error. reads "when <condition> add <item>". `when` accepts `Condition | bool`: a plain bool is a declaration-time constant, fine for genuinely static predicates

- `iff(c1, a1, c2, a2, …[, e])` — exhaustive choice among condition-gated alternatives: `(condition, item)` pairs flattened in order, optionally followed by one trailing else item. the first holding condition selects its item; when none holds the else item is selected, and with no else the selection raises — the implicit exhaustiveness guard. every branch condition is evaluated (a typo fails every selection)

- `select(entries, variables)` resolves a list of plain, `when`-wrapped, and `iff` entries against the facts

Consumers:

- a bro's `tools` / `data_sources` entries may be `when`-wrapped or `iff`-grouped; `BaseBro.__init__` selects at harness `bro`, so an unmatched declaration is never applied. E.g. the dev toolset mounts only on the bro harness (claude has built-in file/shell tools): `tools = [when(harness == 'bro', mount(dev_mcp.toolset))]`. The same wrapper gates native blocks: `tools = [when(harness == 'claude', block('Read', 'Write'))]`; selecting a block for the bro harness raises because it has no native tools to remove.

## Facts

The facts triple a conditioning surface knows, exported by `bro/llm/mcp.py` as ready-made placeholders (`from bro.llm.mcp import creds, harness, wire`):

- `harness` — which toolset drives the work: `bro` (bro-native LLM runs and `--raw` claude sessions) or `claude` (Claude Code's own harness with its built-in tools)

- `wire` — how the surface spells canonical `namespace::tool` names: `bare` (`namespace__tool`, the bro-native LLM loop) or `mcp` (`mcp__namespace__tool`, any claude session). Orthogonal to `harness` — a `--raw` session runs the bro harness over mcp wire names

- `creds` — the set of secrets the environment resolves. The supplied universe is closed (the registry's known names) and membership probes `bro.base.credentials.available` lazily

One more fact sits outside the triple: `hold` — the session's user-involvement level (`unattended | detached | attended | guided`, the domain is `bro.llm.mcp.HOLDS`). It is supplied only when rendering the hold text (`bro.prompts.hold_fragment` → `render_text(hold=…)`), never by the general conditioning surfaces, so hold-neutral text — scripts, procedure docs — fails fast on a stray `#hold` directive. No ready-made placeholder is exported.

`bro.llm.mcp.select(entries, harness=…, wire=…, creds=…)` owns the facts-to-variables mapping for declarative lists (`bro.llm.mcp.render_text` is its sibling for text — see `bro/reference/template.md`). Both accept `extra` — a caller-owned vocabulary merged next to the facts (bro features, below). A fact the surface doesn't know defines no variable, so a condition referencing it raises. Select in the process that consumes the result, where the credential store is the session's own.

## Bro features

A bro may declare named optional capabilities: `features = {'brog': creds.contains('brog')}` on the class — feature name → the gate deciding whether the feature is on: a `Condition`, or a plain bool constant as in `when` (`True` pins it on, `False` disables it). The map is MRO-merged with derived classes overriding per name, and `False` is terminal: redeclaring a feature a base class disabled as anything but `False` fails construction, so an opt-out binds the whole sub-hierarchy. The declaration adds a `#features` variable (`BaseBro.vocabulary()`, passed as the fronts' `extra`) to everything the bro renders or assembles — `tools` / `data_sources` selection, prompt composition, script bodies, cw's append prompt. Components gate with `when(feature('brog'), …)` (`from bro.bro import feature`) and text with `{{iff #features contains brog}}`, so one declaration switches every consuming site together, and a gated component enters the credential manifest only where its gates resolve.

The `#features` universe is the declared feature names — environment-independent, so a typo'd name fails every render. A gate condition evaluates against its own single-variable vocabulary — `creds`, probing `bro.base.credentials.available` lazily with no closed universe — deliberately not the surface's `#creds` fact: that fact's closed universe is the store's registry, which in a scoped container omits never-hydrated names, so a probe there would raise a universe violation, while the gate vocabulary's open universe makes the same probe read as feature-off. Any other variable reference in a gate raises. The condition model has no conjunction, so a feature needing several secrets has no gate spelling until an `and` combinator exists.

## Server-domain vocabularies

The harness facts above condition *session-level* text — prompts, script bodies. An MCP server's own tool text (descriptions, parameter annotations) deliberately does not use them: a server must read the same served standalone, so it renders at build time against its own vocabulary, and no unprocessed directive ever leaves a server (`bro.llm.mcp.FunctionTool`'s `variables`):

- `tools` — a `Toolset` build's selected roster; universe is the full definition, so a description can test an excluded sibling (`{{when #tools contains read_reference}}…{{end}}`) and a typo'd name fails the build
- `features` — a data source's capability set (e.g. a searchable source's `summary`, live iff its LLM key resolves); membership probes lazily at render, universe is the source's declared `feature_names`. The source's own name rides along as `source` (for `{{insert #source}}`)

The one exception is the `bro` service-tool build: service tools are harness features, so it injects the system `#wire` fact (`bro.llm.mcp.surface_variables`) next to its `#tools` roster.

## Code map

- `bro/base/condition.py` — the model: typed variables, `Variable` operators, the evaluator, `when` / `iff` / `select`
- `bro/llm/mcp.py` — `select` / `render_text` and the fact placeholders: the surface-facts front (`Harness`, `Wire`, credentials)
- `bro/base/template.py` — the text front, lowering `{{…}}` directives onto this model (`bro/reference/template.md`)
