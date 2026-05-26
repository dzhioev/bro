# Bro

A **Bro** is a specialised agent: a system prompt, a curated set of tools, and an LLM loop that turns inputs into outputs. Each Bro encapsulates one capability — triaging the Notion inbox, stewarding the media library, answering general questions — and exposes the same uniform interface regardless of where it is invoked from.

This document is the conceptual model. Operational details (layout, how to add a new Bro, current files) live in `CLAUDE.md`.

## Principles

- **One capability per Bro.** A Bro is named, described, and prompted for exactly one job. Composition happens externally — an orchestrator Bro delegates to specialists via the same abstractions used everywhere else.
- **Declarative tool composition.** A Bro lists its tool sources on the class (`data_sources`, `mcp_servers`); the base class materialises and mounts them. No per-Bro wiring code.
- **Stateless by default.** `run()` is a fresh agent loop per call — no history carried over, no hidden state. `send()` opts into multi-turn continuity when the surface needs it.
- **Delivery-agnostic.** The same Bro instance is invoked from a CLI, an HTTP server, a Claude Code session, a sub-agent call, or a scheduled job. The launcher picks the entry point; the Bro does not care.
- **Policy lives in the system prompt.** What the Bro decides is in `system_prompt`; how it acts is in its tools. There is no third place. Consumers of the policy (other surfaces, documentation) reference the Bro, they do not restate it.

## Anatomy

A Bro is a subclass of `Bro`:

```python
class PM(Bro):
  name = 'pm'                          # unique, kebab-case
  description = '...'                  # one line, shown in tool listings
  model = 'gpt-5.4-mini'               # override the base default
  reasoning_effort = 'medium'          # optional
  mcp_servers = [flow.MCPServer()]     # full flow toolset
  # or: flow.MCPServer('add_task', 'list_tasks')  # subset, validated at construction
  data_sources = [Wikipedia()]         # read-only connectors

  def __init__(self):
    super().__init__(system_prompt=SYSTEM_PROMPT)
```

The base class, on instantiation:

- materialises any `mcp_servers` entry that is a factory (callable) — instances pass through unchanged
- auto-prepends every `prompts/shared/*.md` to the system prompt (conventions that must hold across every surface live there)
- appends a `## Data sources` block describing each declared `DataSource`
- on non-interactive runs, exposes a built-in `raise` service tool (see below)

## Stateless vs stateful

- `run(input) -> str` — single-turn. Fresh LLM, fresh tool set, no history. Used by `bro run`, sub-agent invocations, and any one-shot launcher.
- `send(message) -> str` — multi-turn. Lazily creates an LLM on the first call (with system prompt + message), reuses it across subsequent calls (message only). Continuity comes from the OpenAI Responses API's `previous_response_id`, not from re-sending the message history.
- `map(inputs, max_concurrency=5) -> list[str]` — parallel over `run`. Each clone gets its own LLM and tool set; results return in input order, bounded by `max_concurrency`.

Cloning a stateless Bro is free, so `map` is a bounded `asyncio.gather` over `run`.

## Tools and data

A Bro's behaviour comes from three sources, all declared on the class:

- **`system_prompt`** — the specialisation. Triage policies, output protocol, voice. The single source of truth for what the Bro knows and decides.
- **`mcp_servers`** — sets of stateful tools. Each entry is either an `MCPServer` instance (typically `flow.MCPServer(*tool_names)` or `infra.MCPServer(*tool_names)`; no args = full toolset, with args = a validated subset built directly with only those tools) or a `() -> MCPServer` factory for the rare server that needs per-instance materialisation.
- **`data_sources`** — read-only connectors (Wikipedia, TMDb, Open Library, web search). Each implements `search(query, limit)` and `fetch(id, query=None)`. The base class exposes them as `<name>-search` / `<name>-fetch` MCP tools and injects each source's `summary` into the system prompt so the LLM knows what is available without enumerating raw tool names.

The split between `mcp_servers` and `data_sources` is a contract: data sources never mutate state, so they are safe to bind to any Bro. MCP servers may mutate state and are chosen per Bro.

## Sub-agents

A Bro can be exposed as an MCP tool that other Bros call. Two shapes, both in `bro/bro.py`:

- **`Tool`** — single-input wrapper. The calling LLM passes one `input` string; the sub-Bro's final text comes back as the tool result.
- **`ScatterTool`** — parallel map. The calling LLM passes a list of inputs; the sub-Bro runs each concurrently; results come back as a JSON array.

Composition is just mounting a server that exposes the chosen sub-Bros. `ScatterTool`'s parallelism is invisible to the caller — it is a regular MCP tool that fans out internally.

## The `raise` service tool

Non-interactive Bro invocations cannot ask a follow-up question. To let an agent abort cleanly when the request cannot be fulfilled — missing credentials, no appropriate tool or data source, contradictory constraints — the base class exposes a built-in `raise` tool on `run()`. Calling it raises `BroRaised(reason)` out of `run()`; the reason surfaces to the caller as the failure cause. The system prompt is augmented with a non-interactive note so the agent knows it cannot negotiate.

Interactive paths (`Bro.send()`, the HTTP server, `cw ss --bro` Claude Code sessions) do **not** expose `raise` — the agent describes any blocker in its reply and the human decides what to do next.

## Execution surfaces

The same Bro runs from many launchers:

- **Console** — `bro run <name> <input>`, `bro list`, `bro show <name>`. Backed by `bro/run.py`.
- **Sub-agent** — invoked by another Bro via `Tool` / `ScatterTool`.
- **HTTP** — `bro/server/server.py` serves the `assistant` Bro on `POST /v1/chat/completions` (OpenAI-compatible). The iOS chat app speaks to this endpoint.
- **Claude Code** — `cw ss --bro <name>` launches a bare Claude Code session whose system prompt and MCP servers come from the named Bro. Tools are wired through the `mcp-server bro:<name>` stdio shim, which exposes the union of the Bro's declared MCP servers and data-source tools. Useful when the user wants a chat UI over the Bro's policy + toolkit.
- **`ask` / `do-task`** — one-shot launchers for autonomous runs; see `do/CLAUDE.md`.

A given Bro need not support every surface — `pm` is consumed by both `cw ss --bro pm` and the `process-inbox` TUI; `assistant` is HTTP-only; `librorian` runs as console + sub-agent.

## Registry

Bros live in a process-wide dict keyed by `name`. First lookup triggers `bros.init()`, which imports each module and registers its class. There is no auto-discovery; the import plus the registry entry is the contract. Each subclass is instantiated exactly once at registration time, and every caller gets that same instance.

## Concrete roster

The current set lives in `bros/`:

- `assistant` — general-purpose chat; default for the iOS app via the HTTP server
- `pm` — Flow inbox triage; canonical source of triage policy for both the `process-inbox` TUI and `cw ss --bro pm`
- `librorian` — steward of the Flow media library (adds, maintains, recommends)
- `devoops` — autonomous service deploys (flow-mcp, flow-focus, emails-test, emails-prod) with a dry-run-first safety reflex; tools wrap `infra/mcp.py`

Adding a new Bro is creating a new subclass and registering it — see `CLAUDE.md` for the operational checklist.
