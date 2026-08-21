# Bro

A **Bro** is a specialised agent:
a system prompt, a curated set of tools, and an LLM loop that turns inputs into outputs.
Each Bro encapsulates one capability
— reviewing code, researching a topic, answering general questions
— and exposes the same uniform interface regardless of where it is invoked from.

This document is the conceptual model.
Operational details (layout, how to add a new Bro, current files) live in `AGENTS.md`.

## Principles

- **One capability per Bro.**
  A Bro is named, described, and prompted for exactly one job.
  Composition happens externally — an orchestrator Bro delegates to specialists via the same abstractions used everywhere else.
- **Declarative tool composition.**
  A Bro lists its tool policy on the class (`tools`, `data_sources`);
  the base class selects server mounts and harness-native blocks for each surface, materialising live servers lazily on first tool use.
  No per-Bro wiring code.
- **Stateless by default.**
  `run()` is a fresh agent loop per call
  — no history carried over, no hidden state.
  `send()` opts into multi-turn continuity when the surface needs it.
- **Delivery-agnostic.**
  The same Bro instance is invoked from a CLI, an HTTP server, a Claude Code session, or a scheduled job.
  The launcher picks the entry point;
  the Bro does not care.
- **Policy lives in the system prompt.**
  What the Bro decides is in `system_prompt`;
  how it acts is in its tools.
  There is no third place.
  Consumers of the policy (other surfaces, documentation) reference the Bro, they do not restate it.

## Anatomy

A Bro is a subclass of `Bro`:

```python
class Researcher(Bro):
  name = 'researcher'                   # unique, kebab-case
  description = '...'                  # one line, shown in tool listings
  system_prompt = SYSTEM_PROMPT        # class-level; MRO-concatenated base→derived
  llm_spec = bro.llm.llms.openai.LLMSpec(model='gpt-5.6-sol', reasoning_effort='medium')
  tools = [mount(research.mcp.toolset)]
  # or: mount(research.mcp.toolset, 'search')  # validated subset
  data_sources = [Wikipedia()]         # read-only connectors
```

Everything is a class attribute
— there is no custom `__init__`.
The LLM recipe is the whole `LLMSpec` (model + provider-specific knobs), overridden as one attribute;
per-instance overrides go through `Bro.create(spec)`.

The base class, on instantiation:

- selects the `tools` declarations for the active harness;
  live servers are built once, lazily, when the bro first runs tools
- auto-prepends every `bro/prompts/shared/*.md` to the system prompt (conventions that must hold across every surface live there)
- appends a `## Data sources` block describing each declared `DataSource`
- on non-interactive runs, exposes a built-in `raise` service tool (see below)

## Stateless vs stateful

- `run(input) -> str` — single-turn.
  Fresh LLM, fresh tool set, no history.
  Used by `bro run` and any one-shot launcher.
- `send(message) -> str` — multi-turn.
  Lazily creates an LLM on the first call (with system prompt + message), reuses it across subsequent calls (message only).
  Continuity comes from the OpenAI Responses API's `previous_response_id`, not from re-sending the message history.

## Tools and data

A Bro's behaviour comes from three sources, all declared on the class:

- **`system_prompt`** — the specialisation.
  Triage policies, output protocol, voice.
  The single source of truth for what the Bro knows and decides.
- **`tools`** — one checkable `ToolLayer` type for every entry.
  `mount(research.mcp.toolset, *tool_names)` contributes a full or validated subset and carries its pure-metadata server manifest (`needed_secrets` / `optional_secrets` plus a `build()` factory);
  `block(*tool_names)` restricts native tools on a harness that supplies its own, `allow_commands(tool_name, *commands)` hands one back narrowed
  — a native tool that takes a command line, served but reaching only the declared commands, so a Bro can hold a capability without the shell underneath it
  — and `serve(*tool_names)` hands one back whole, for a tool with no command line to narrow on.
  All produce layers and use the same `when` / `iff` conditioning.
  Declaring native tools for a harness that has none is a declaration error, as is handing back one the Bro does not also block.
- **`data_sources`** — read-only connectors (Wikipedia, web search, and consumer-provided catalogs).
  Each implements `search(query, limit)` and `fetch(id, query=None)`.
  The base class exposes them as `search` / `fetch` MCP tools inside the source's own `<name>-source` namespace (wire name `<name>-source__search`)
  and injects each source's `summary` into the system prompt so the LLM knows what is available without enumerating raw tool names.

The split between `tools` and `data_sources` is a contract:
data sources never mutate state, so they are safe to bind to any Bro.
Tool servers may mutate state and are chosen per Bro.

A Bro can additionally declare **spells**
— named procedures backed by markdown files under `<bro_pkg>/spells/*.md` with flat frontmatter.
Spells compose along the MRO (derived overrides parent) and mount as one tool each in the reserved `spell` namespace for bro-native execution and both Claude session flavors.
When OpenAI is available, the `bro::cast` service tool interprets free-form commands against that roster and returns the resolved spell's instructions with the interpreted arguments, or an expected error;
direct tools remain available without it.
Text enclosed as `[[…]]` names a spell
— in a user message, a spell body, or a doc, phrased to fit its sentence
— and is run only where the sentence asks for it.
The composed Spells prompt carries the direct and natural-language invocation contracts, while `bro show` exposes the roster and OpenAI is a best-effort secret for any bro with spells.
Native bro and `ride solo|along --raw` also mount framework service tool `bro::skill(name)` as the bridge for third-party skills their harness cannot load;
an empty body represents an unavailable skill.
Ordinary Claude persona sessions use Claude's native skill mechanism and do not adapt bro spells into skills.

## The `raise` service tool

Non-interactive Bro invocations cannot ask a follow-up question.
To let an agent abort cleanly when the request cannot be fulfilled
— missing credentials, no appropriate tool or data source, contradictory constraints
— the base class exposes a built-in `raise` tool at the unattended hold, `run()`'s default.
Calling it raises `BroRaised(reason)` out of `run()`;
the reason surfaces to the caller as the failure cause.
The system prompt is augmented with the matching hold fragment, so an unattended agent knows it cannot negotiate.

Interactive paths (`send()`, guided `ride solo|along --raw` Claude Code sessions) do **not** expose `raise`
— the agent describes any blocker in its reply and the human decides what to do next.

## Execution surfaces

The same Bro runs from many launchers:

- **Console** — `bro run <name> <input>`, `bro list`, `bro show <name>`.
  Backed by `bro/run.py`.
- **Claude Code raw** — `ride along <name> --raw` launches a bare Claude Code session whose system prompt and MCP servers come from the session's Bro.
  Tools are served by a session-local HTTP MCP server (`mcp-server bro:<name> --http`) exposing the union of the Bro's declared MCP servers, data-source tools, spells, and the framework `bro::skill` loader, one endpoint per namespace.
  Useful when the user wants a chat UI over the Bro's policy + toolkit.
- **Claude Code persona** — every non-raw `ride solo|along <name>` Claude session runs *as* its selected Bro too:
  the Bro's persona and Spells prompts are injected, its spells mount as canonical `spell::` tools,
  Claude retains its native third-party skill mechanism, and the bro's Claude-harness-filtered toolset (`ride.claude.assembly.persona_servers()` via `mcp-server persona:<name> --http`) mounts alongside Claude's remaining built-in tools
  — components gated to the bro harness (the dev toolset) stay out, and selected blocks feed Claude's `--disallowed-tools`.
- **`bro run` / `bro chat`**
  — canonical one-shot and interactive launchers;
  `ask` and `call` are aliases.
  See `bro/launch/AGENTS.md`.

A given Bro need not support every surface.
A consumer can invoke one from its own application, expose another only through `bro run`, and use a third as a `ride` persona without changing the Bro abstraction.

## Registry

Bros live in a process-wide dict keyed by `name`, holding **classes** (not instances).
Lookup is lazy per-name:
a miss reads the `name -> "module:ClassName"` declarations from installed distribution metadata, imports only the one module named for that name, and registers its class, so `create_bro('researcher')` never pulls in another bro's dependency graph.
There is no `bros.init()` and no auto-discovery;
the entry-point declaration is the contract (only `list_classes()` / `bro list` imports them all).
`create_bro(name, llm_spec=None)` returns a **fresh instance every call**
— callers that need the same instance across requests cache the return value themselves.

## Concrete roster

The core distribution ships only `bro`, the minimal general-purpose agent and base for other concrete Bros.
`bro-dev` contributes the development-domain personas (`dev`, `lead`, `terminal`, and `analyst`);
other distributions contribute consumer personas through the same `bro` entry-point group.

Adding a new Bro is creating a new subclass and registering it
— see `AGENTS.md` for the operational checklist.
