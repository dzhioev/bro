# bro/CLAUDE.md

Bro is the agent system: independent specialised agents (a "Bro") each run as a stateless LLM loop with their own system prompt and MCP-tool access. Conceptual model and rationale: `DESIGN.md`. Run any script with `--help` for flags.

## Layout

- `bro.py` — `Bro` ABC. Subclasses set `name`, `description`, `system_prompt`, and optionally class-level `data_sources = [...]` and `mcp_servers = [...]` (factories, or `McpServerSpec(factory, allowed_tools=[...])` for per-tool allowlists). `Bro` materialises each factory once per instance, auto-prepends every `prompts/shared/*.md` to the system prompt, and appends a `## Data sources` block describing each declared `DataSource`. Non-interactive runs (`bro.run()`, the `bro run`/`do`/`do-task` CLIs, sub-Bros invoked via `Tool`) also expose a built-in `raise` service tool — the agent calls it to abort with a reason when the request cannot be fulfilled (missing credentials, no appropriate tool, contradictory constraints, unclear/uninterpretable input); the call raises `BroRaised(reason)` out of `bro.run()`. Interactive paths (`bro.send()`, the HTTP server backing the iOS app) don't expose `raise` — the agent should just describe any blocker in its reply. Post-init injection of extra servers is available via `extend_mcp_servers(...)` for tests or unusual hosts
- `bros/` — one file per specialised agent; `bros/__init__.py:init()` registers each with the registry. Currently `assistant.py` (general-purpose), `pm.py` (Flow inbox triage), `librorian.py` (steward of the Flow media library — adds, maintains, and recommends), and `devoops.py` (autonomous service deploys with a dry-run safety reflex; tools live in `infra/mcp.py`)
- `datasources/` — `DataSource` ABC + connectors to read-only sources (books, films, web references). Each connector exposes `<name>-search` / `<name>-fetch` tools via `as_mcp_server()`. Currently `wikipedia.py` (Wikipedia REST API, query-aware fetch via `mu`), `tmdb.py` (movies + series; needs `.configs/tmdb.json`), `open_library.py` (books, no auth), `web_search.py` (Brave Search; needs `.configs/brave.json`)
- `registry.py` — process-wide registry (`register`, `get_bro`, `list_bros`); first lookup triggers `bros.init()`
- `run.py` (`bro`) — CLI: `bro run <name> <input>`, `bro list`, `bro show <name> [--system-prompt]` (markdown info card; renderer in `show.py`)
- `server/server.py` (`bro.server.server`) — aiohttp wrapper exposing the `assistant` Bro on `POST /v1/chat/completions` (OpenAI-compatible). Used by the iOS app
- `app/ios/` — iOS chat client; see `app/ios/CLAUDE.md`

## PM Bro

`bros/pm.py` is the single source of truth for Flow inbox triage policy — status taxonomy, importance/driver semantics, every per-kind policy (payment receipts, subscription renewals, bill payments, media items, title metadata extraction, sphere tags, …). Its `SYSTEM_PROMPT` is canonical: do not duplicate the policies elsewhere. Two delivery surfaces consume it:

- `process-inbox` TUI (`flow/process_inbox.py`) — Textual app; instantiates `PM()` with no MCP servers and constrains it to a JSON suggestion protocol so the user previews every change before it applies. Engine details in `flow/process_inbox.REFERENCE.md`
- `cw ss --bro pm` — Claude Code session with PM's system prompt and the flow MCP toolkit; PM calls `update_task` / `add_task` itself (no preview). Use when you want to triage from a chat UI rather than the TUI

Changes to triage policy go in `bro/bros/pm.py` and propagate to both surfaces on next process start.

## Adding a Bro

Create `bros/<name>.py` with a `Bro` subclass (`name`, `description`, `system_prompt`). Declare required tool sources on the class:

- `data_sources = [YourSource()]` for read-only data connectors
- `mcp_servers = [create_flow_server]` for full-server mounts (the entry is a factory — `() -> MCPServer`)
- `mcp_servers = [McpServerSpec(create_flow_server, allowed_tools=['add_task', 'list_tasks'])]` to restrict which tools the Bro sees

The flow MCP factory is `flow.mcp.bridge.create_flow_server`.

**Register the new bro manually** — import `YourBro` in `bros/__init__.py:init()` and append it to the list iterated there. There is no auto-discovery; the import + list entry is required for `get_bro('your-name')` to work.

## Adding a DataSource

Subclass `bro.datasources.base.DataSource`, set `name` (slug) and `summary` (one-line; injected into the system prompt of every Bro that uses it), and implement `async search(query, limit) -> list[Hit]` and `async fetch(id, query=None) -> str`. `fetch` receives the original user query so the connector can return a focused summary (see `wikipedia.py` for the `mu`-based pattern). Bind to a Bro by declaring `data_sources = [YourSource()]` on its class.
