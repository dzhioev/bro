# Bro

A **Bro** is a specialised agent: a system prompt, a curated set of tools, and an LLM loop that turns inputs into outputs. Each Bro encapsulates one capability — triaging the Notion inbox, stewarding the media library, answering general questions — and exposes the same uniform interface regardless of where it is invoked from.

This document is the conceptual model. Operational details (layout, how to add a new Bro, current files) live in `CLAUDE.md`.

## Principles

- **One capability per Bro.** A Bro is named, described, and prompted for exactly one job. Composition happens externally — an orchestrator Bro delegates to specialists via the same abstractions used everywhere else.
- **Declarative tool composition.** A Bro lists its tool sources on the class (`data_sources`, `mcp_servers`); the base class mounts them, materialising live servers lazily on first tool use. No per-Bro wiring code.
- **Stateless by default.** `run()` is a fresh agent loop per call — no history carried over, no hidden state. `send()` opts into multi-turn continuity when the surface needs it.
- **Delivery-agnostic.** The same Bro instance is invoked from a CLI, an HTTP server, a Claude Code session, or a scheduled job. The launcher picks the entry point; the Bro does not care.
- **Policy lives in the system prompt.** What the Bro decides is in `system_prompt`; how it acts is in its tools. There is no third place. Consumers of the policy (other surfaces, documentation) reference the Bro, they do not restate it.

## Anatomy

A Bro is a subclass of `Bro`:

```python
class PM(Bro):
  name = 'pm'                          # unique, kebab-case
  description = '...'                  # one line, shown in tool listings
  system_prompt = SYSTEM_PROMPT        # class-level; MRO-concatenated base→derived
  llm_spec = llm.llms.chat_gpt.LLMSpec(model='gpt-5.4-mini', reasoning_effort='medium')
  mcp_servers = [flow.mcp.spec()]          # full flow toolset
  # or: flow.mcp.spec('add_task', 'list_tasks')  # subset, validated at declaration
  data_sources = [Wikipedia()]         # read-only connectors
```

Everything is a class attribute — there is no custom `__init__`. The LLM recipe is the whole `LLMSpec` (model + provider-specific knobs), overridden as one attribute; per-instance overrides go through `Bro.create(spec)`.

The base class, on instantiation:

- keeps the `mcp_servers` specs as declared; live servers are built once, lazily, when the bro first runs tools
- auto-prepends every `prompts/shared/*.md` to the system prompt (conventions that must hold across every surface live there)
- appends a `## Data sources` block describing each declared `DataSource`
- on non-interactive runs, exposes a built-in `raise` service tool (see below)

## Stateless vs stateful

- `run(input) -> str` — single-turn. Fresh LLM, fresh tool set, no history. Used by `bro run` and any one-shot launcher.
- `send(message) -> str` — multi-turn. Lazily creates an LLM on the first call (with system prompt + message), reuses it across subsequent calls (message only). Continuity comes from the OpenAI Responses API's `previous_response_id`, not from re-sending the message history.

## Tools and data

A Bro's behaviour comes from three sources, all declared on the class:

- **`system_prompt`** — the specialisation. Triage policies, output protocol, voice. The single source of truth for what the Bro knows and decides.
- **`mcp_servers`** — sets of stateful tools. An entry is a tool-pack module (`flow.mcp`, `infra.mcp`, `dev.mcp` — its conventional `spec` Toolset, the full roster) or a scoping call (`flow.mcp.spec(*tool_names)`, a validated subset); both normalize to an `llm.mcp.MCPServerSpec`, the pure-metadata manifest (`needed_secrets` / `optional_secrets` plus a `build()` factory). The declaration/runtime split lets hosts read a bro's credential manifest without constructing live servers — a live server is free to hold real resources (flow's shared `System`) because it only ever exists in a serving process.
- **`data_sources`** — read-only connectors (Wikipedia, TMDb, Open Library, web search). Each implements `search(query, limit)` and `fetch(id, query=None)`. The base class exposes them as `search` / `fetch` MCP tools inside the source's own `<name>-source` namespace (wire name `<name>-source__search`) and injects each source's `summary` into the system prompt so the LLM knows what is available without enumerating raw tool names.

The split between `mcp_servers` and `data_sources` is a contract: data sources never mutate state, so they are safe to bind to any Bro. MCP servers may mutate state and are chosen per Bro.

A Bro can additionally declare **skills** — named procedures backed by markdown files under `<bro_pkg>/skills/*.md` with YAML frontmatter. Skills compose along the MRO like `mcp_servers` (derived overrides parent on name), the base class auto-appends a `## Available skills` block to the system prompt, and a built-in `skill(name)` service tool returns the named skill's body for the LLM to follow. Skill files use the same `SKILL.md` shape Claude Code consumes, so the same file is the source of truth across surfaces.

## The `raise` service tool

Non-interactive Bro invocations cannot ask a follow-up question. To let an agent abort cleanly when the request cannot be fulfilled — missing credentials, no appropriate tool or data source, contradictory constraints — the base class exposes a built-in `raise` tool at the unattended hold, `run()`'s default. Calling it raises `BroRaised(reason)` out of `run()`; the reason surfaces to the caller as the failure cause. The system prompt is augmented with the matching hold fragment, so an unattended agent knows it cannot negotiate.

Interactive paths (`Bro.send()`, the HTTP server, guided `cw ss --bro` Claude Code sessions) do **not** expose `raise` — the agent describes any blocker in its reply and the human decides what to do next.

## Execution surfaces

The same Bro runs from many launchers:

- **Console** — `bro run <name> <input>`, `bro list`, `bro show <name>`. Backed by `bro/run.py`.
- **HTTP** — `bro/server/server.py` serves the `assistant` Bro on `POST /v1/chat/completions` (OpenAI-compatible). The iOS chat app speaks to this endpoint.
- **Claude Code** — `cw ss --bro <name>` launches a bare Claude Code session whose system prompt and MCP servers come from the named Bro. Tools are served by a session-local HTTP MCP server (`mcp-server bro:<name> --http`) exposing the union of the Bro's declared MCP servers and data-source tools, one endpoint per namespace. Useful when the user wants a chat UI over the Bro's policy + toolkit.
- **Claude Code persona** — every cw-session (the default non-`--bro` `cw ss` flavor) runs *as* a Bro too (`--persona <name>`, defaulting to the project default bro): the Bro's persona prompt is injected, its skills become slash commands, and its claude-harness-filtered toolset (`claude_persona_mcp_servers()` via `mcp-server persona:<name> --http`) mounts alongside claude's built-in tools — components gated to the bro harness (the dev toolset) stay out.
- **`bro run` / `bro chat`** — canonical one-shot and interactive launchers; `ask` and `call` are aliases. See `bro/launch/CLAUDE.md`.

A given Bro need not support every surface — `pm` is consumed by both `cw ss --bro pm` and the `process-inbox` TUI; `librorian` runs from the console. `assistant` (which declares `mcp_servers=[flow.mcp.spec()]`) is reachable from every surface — `bro run`/`bro show`, `cw ss --bro assistant`, and the HTTP server — but it is only the HTTP server's *default* bro, so the iOS app reaches it without naming it.

## Registry

Bros live in a process-wide dict keyed by `name`, holding **classes** (not instances). Lookup is lazy per-name: a miss imports only the one module named for that name in the `BRO_SPECS` map (`name -> "module:ClassName"`) and registers its class, so `create_bro('pm')` never pulls in another bro's dependency graph. There is no `bros.init()` and no auto-discovery; the `BRO_SPECS` entry is the contract (only `list_classes()` / `bro list` imports them all). `create_bro(name, llm_spec=None)` returns a **fresh instance every call** — callers that need the same instance across requests cache the return value themselves.

## Concrete roster

The current set lives in `bros/`:

- `assistant` — general-purpose chat; default for the iOS app via the HTTP server
- `pm` — Flow inbox triage; canonical source of triage policy for both the `process-inbox` TUI and `cw ss --bro pm`
- `librorian` — steward of the Flow media library (adds, maintains, recommends)
- `devoops` — autonomous service deploys (the deploy targets are enumerated in `infra/mcp.py`'s `TARGETS` / `list_targets`) with a dry-run-first safety reflex; tools wrap `infra/mcp.py`
- `dev` — generic software developer with file + shell + search tools
- `ppp-dev` — full-stack PPP development (inherits `dev`, adds the flow toolset and the `/fix`, `/pr`, `/land` skills); this repo's default bro

Adding a new Bro is creating a new subclass and registering it — see `CLAUDE.md` for the operational checklist.
