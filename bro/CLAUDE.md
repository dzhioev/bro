# bro/CLAUDE.md

Bro is the agent system: independent specialised agents (a "Bro") each run as a stateless LLM loop with their own system prompt and MCP-tool access. Conceptual model and rationale: `DESIGN.md`. Run any script with `--help` for flags.

## Layout

- `bro.py` — `Bro` ABC. Subclasses set `name`, `description`, `system_prompt`, and optionally class-level `data_sources = [...]` and `mcp_servers = [...]` (factories, or `McpServerSpec(factory, allowed_tools=[...])` for per-tool allowlists). `Bro` materialises each factory once per instance, auto-prepends every `prompts/shared/*.md` to the system prompt, and appends a `## Data sources` block describing each declared `DataSource`. Post-init injection of extra servers is available via `extend_mcp_servers(...)` for tests or unusual hosts
- `bros/` — one file per specialised agent; `bros/__init__.py:init()` registers each with the registry. Currently `assistant.py` (general-purpose), `pm.py` (Flow inbox triage), and `librorian.py` (research assistant over data sources)
- `datasources/` — `DataSource` ABC + connectors to read-only sources (books, databases, web references). Each connector exposes `<name>-search` / `<name>-fetch` tools via `as_mcp_server()`. Currently `wikipedia.py` (Wikipedia REST API, query-aware fetch via `mu`)
- `registry.py` — process-wide registry (`register`, `get_bro`, `list_bros`); first lookup triggers `bros.init()`
- `run.py` (`bro`) — CLI: `bro run <name> --input ...` and `bro list`
- `server/server.py` (`bro.server.server`) — aiohttp wrapper exposing the `assistant` Bro on `POST /v1/chat/completions` (OpenAI-compatible). Used by the iOS app
- `app/ios/` — iOS chat client; see `app/ios/CLAUDE.md`

## Adding a Bro

Create `bros/<name>.py` with a `Bro` subclass (`name`, `description`, `system_prompt`). Declare required tool sources on the class:

- `data_sources = [YourSource()]` for read-only data connectors
- `mcp_servers = [create_flow_server]` for full-server mounts (the entry is a factory — `() -> MCPServer`)
- `mcp_servers = [McpServerSpec(create_flow_server, allowed_tools=['add_task', 'list_tasks'])]` to restrict which tools the Bro sees

The flow MCP factory is `flow.mcp.bridge.create_flow_server`.

**Register the new bro manually** — import `YourBro` in `bros/__init__.py:init()` and append it to the list iterated there. There is no auto-discovery; the import + list entry is required for `get_bro('your-name')` to work.

## Adding a DataSource

Subclass `bro.datasources.base.DataSource`, set `name` (slug) and `summary` (one-line; injected into the system prompt of every Bro that uses it), and implement `async search(query, limit) -> list[Hit]` and `async fetch(id, query=None) -> str`. `fetch` receives the original user query so the connector can return a focused summary (see `wikipedia.py` for the `mu`-based pattern). Bind to a Bro by declaring `data_sources = [YourSource()]` on its class.
