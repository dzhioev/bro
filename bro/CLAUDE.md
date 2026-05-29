# bro/CLAUDE.md

Bro is the agent system: independent specialised agents (a "Bro") each run as a stateless LLM loop with their own system prompt and MCP-tool access. Conceptual model and rationale: `DESIGN.md`. Run any script with `--help` for flags.

## Layout

- `bro.py` — `BaseBro` ABC (the framework) and the framework helpers (`Tool`, `ScatterTool`, `BroRaised`).

  Subclasses set `name`, `description`, and class-level `system_prompt = "..."` plus optionally `data_sources = [...]` and `mcp_servers = [...]`. Each `mcp_servers` entry is either:
  - an `MCPServer` instance (typically `flow.MCPServer(...)` / `infra.MCPServer(...)`), or
  - a `() -> MCPServer` factory for stateful servers that need per-instance materialisation.

  `BaseBro.__init__` walks the MRO from base to most-derived class and **concatenates each class's own `mcp_servers` and `system_prompt`** — so `class PPPDev(Dev): mcp_servers = [flow.MCPServer()]` declares only what PPPDev *adds*; Dev's own `mcp_servers` entry (the dev MCP server) flows through automatically (same for `system_prompt`). The legacy `super().__init__(system_prompt=...)` escape hatch remains for callers that need a dynamic prompt (e.g. PM injects current local time at instantiation).

  `BaseBro` also auto-prepends every `prompts/shared/*.md` to the system prompt and appends a `## Data sources` block describing each declared `DataSource`.

  **Model and LLM settings.** `model` (default `DEFAULT_MODEL`) and `reasoning_effort` are class attributes. `llm_settings` is a class-level dict of LLM-specific knobs (e.g. `{'service_tier': 'priority'}`) that `__init__` MRO-merges (base→derived, derived wins) like `mcp_servers` and splats straight into the LLM constructor — adding a new knob touches only the LLM class, never `BaseBro` or `get_llm`. Construction-time overrides go through the `BaseBro.create(llm)` classmethod factory, which takes an `LLMSpec` (a `model` paired with its `settings`; settings are model-specific, e.g. `service_tier` is OpenAI-only, so they always travel bound to a model) and applies it *after* the bro's own `__init__` — so subclass constructors never have to forward anything. The override settings win over the class default, including over the class `reasoning_effort`.

  **Interactive vs non-interactive paths.** Non-interactive runs (`bro.run()`, the `bro run` / `ask` / `do-task` CLIs, sub-Bros invoked via `Tool`) expose a built-in `raise` service tool — the agent calls it to abort with a reason when the request cannot be fulfilled (missing credentials, no appropriate tool, contradictory constraints, unclear/uninterpretable input); the call raises `BroRaised(reason)` out of `bro.run()`. Interactive paths (`bro.send()`, the HTTP server backing the iOS app, the `call` CLI) don't expose `raise` and inject a symmetric interactive-mode note instead — the agent is told to ask clarifying questions rather than guess, and to describe any blocker in its reply.

  **Tracing.** Non-interactive runs stream a live trace of reasoning summaries, tool calls, tool outputs, and interim messages to stderr — timestamped plain log lines via `llm.tracer.BoringTracer` by default, or colored `rich` panels via `RichConsoleTracer` when `--rich` is passed (`ask --rich`, `do-task --rich`, `bro run --rich`); both honor `tracer=` on `BaseBro.run` for explicit overrides. Subclasses override `_make_tracer()` to set the default tracer; test bros return `NullTracer()` for silence.

  Post-init injection of extra servers is available via `extend_mcp_servers(...)` for tests or unusual hosts.
- `bros/` — one package per concrete agent (`bros/<name>/__init__.py`); `bros/__init__.py:init()` registers each with the registry.

  `bros/bro/` defines the concrete `Bro(BaseBro)` — the "default bro" registered as `bro`, with a minimal go-to system prompt and no MCP servers. **All other bros inherit from this `Bro`** (not `BaseBro` directly), so they pick up the shared defaults via the MRO walk; inherit from `BaseBro` only when you want to opt out of those defaults.

  Specialists:
  - `assistant/` — chat + flow tools
  - `pm/` — Flow inbox triage
  - `librorian/` — steward of the Flow media library (adds, maintains, recommends)
  - `devoops/` — autonomous service deploys with a dry-run safety reflex; tools live in `infra/mcp.py`
  - `dev/` — generic developer with file/shell/search tools from sibling `mcp.py` (exposes `read_file`, `write_file`, `edit_file`, `bash`, `grep`, `glob`, `read_reference` as `FunctionTool`s; `bash` and `grep` shell out, file ops are thin Python; `MCPServer(*names)` is the `InProcessMCPServer` subclass — same shape as `flow.MCPServer` / `infra.MCPServer`; see sibling `REFERENCE.md` for the shared output cap / skipped-content markers / fat-finger clamp the LLM fetches via `read_reference`)
  - `ppp_dev/` — PPP-specific developer with the flow toolset and the PPP task-driven workflow
- `datasources/` — `DataSource` ABC + connectors to read-only sources (books, films, web references). Each connector exposes `<name>-search` / `<name>-fetch` tools via `as_mcp_server()`. Currently:
  - `wikipedia.py` — Wikipedia REST API, query-aware fetch via `mu`
  - `tmdb.py` — movies + series; needs `.configs/tmdb.json`
  - `open_library.py` — books, no auth
  - `web_search.py` — Brave Search; needs `.configs/brave.json`
- `registry.py` — process-wide registry (`register`, `get_bro`, `list_bros`); first lookup triggers `bros.init()`
- `run.py` (`bro`) — CLI: `bro run <name> <input>`, `bro list`, `bro show <name> [--system-prompt]` (markdown info card; renderer in `show.py`)
- `server/server.py` (`bro.server.server`) — aiohttp wrapper exposing the `assistant` bro on `POST /v1/chat/completions` (OpenAI-compatible). Used by the iOS app
- `app/ios/` — iOS chat client; see `app/ios/CLAUDE.md`

## PM Bro

`bros/pm/` is the single source of truth for Flow inbox triage policy — status taxonomy, importance/driver semantics, every per-kind policy (payment receipts, subscription renewals, bill payments, media items, title metadata extraction, sphere tags, …). Its `SYSTEM_PROMPT` is canonical: do not duplicate the policies elsewhere. Two delivery surfaces consume it:

- `process-inbox` TUI (`flow/process_inbox.py`) — Textual app; instantiates `PM()` with no MCP servers and constrains it to a JSON suggestion protocol so the user previews every change before it applies. Engine details in `flow/process_inbox.REFERENCE.md`
- `cw ss --bro pm` — Claude Code session with PM's system prompt and the flow MCP toolkit; PM calls `update_task` / `add_task` itself (no preview). Use when you want to triage from a chat UI rather than the TUI

Changes to triage policy go in `bro/bros/pm/__init__.py` and propagate to both surfaces on next process start.

## Adding a Bro

Create `bros/<name>/__init__.py` with `from bro.bros.bro import Bro` and a `class YourBro(Bro)` (inherit from the concrete `Bro` so you pick up the shared defaults; use `BaseBro` only when you want to opt out). Declare `name`, `description`, and `system_prompt` as class attributes; add tool sources as class attributes too:

- `system_prompt = "..."` — class-level. When you `class B(A)` and both declare `system_prompt`, `__init__` concatenates A's then B's (MRO base-to-derived) so subclasses only declare their *additions*.
- `data_sources = [YourSource()]` for read-only data connectors
- `mcp_servers = [flow.MCPServer()]` for the full flow toolset
- `mcp_servers = [flow.MCPServer('add_task', 'list_tasks')]` to scope to specific tools (validated at construction)
- `mcp_servers = [infra.MCPServer()]` for the devops toolset; same `*tool_names` API
- `mcp_servers = [MCPServer()]` (imported from `bro.bros.dev.mcp`) for the developer toolset (read_file/write_file/edit_file/bash/grep/glob)
- Stateful servers that need a fresh instance per Bro can pass a factory: `mcp_servers = [some_factory]` where `some_factory: () -> MCPServer`
- `mcp_servers` is also walked along the MRO and concatenated — `PPPDev(Dev)` with `mcp_servers = [flow.MCPServer()]` ends up with Dev's dev MCP server *plus* `flow.MCPServer()`.
- `model` / `reasoning_effort` set the LLM; `llm_settings = {...}` carries any other LLM-specific knob (MRO-merged, splatted into the LLM constructor). For per-instance overrides use the `create()` factory with an `LLMSpec` — `YourBro.create(LLMSpec('gpt-5', {'service_tier': 'priority'}))` — which applies it after construction, so a bro's own `__init__` never needs to know about it.

**Register the new bro manually** — import `YourBro` in `bros/__init__.py:init()` and append it to the list iterated there. There is no auto-discovery; the import + list entry is required for `get_bro('your-name')` to work.

## Adding a DataSource

Subclass `bro.datasources.base.DataSource`, set `name` (slug) and `summary` (one-line; injected into the system prompt of every Bro that uses it), and implement `async search(query, limit) -> list[Hit]` and `async fetch(id, query=None) -> str`. `fetch` receives the original user query so the connector can return a focused summary (see `wikipedia.py` for the `mu`-based pattern). When an upstream HTTP/network failure makes the source temporarily unusable, raise `bro.datasources.base.SourceUnavailable(source, reason)` rather than letting raw transport exceptions escape — the agent loop turns it into a tool result the model can route around. Bind to a Bro by declaring `data_sources = [YourSource()]` on its class.
