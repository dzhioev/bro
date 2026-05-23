# bro/CLAUDE.md

Bro is the agent system: independent specialised agents (a "Bro") each run as a stateless LLM loop with their own system prompt and MCP-tool access. Conceptual model and rationale: `DESIGN.md`. Run any script with `--help` for flags.

## Layout

- `bro.py` — `Bro` ABC. Subclasses set `name`, `description`, `system_prompt`; instances are wired with MCP servers and/or data sources at registration time. `Bro` auto-prepends every `prompts/shared/*.md` to the system prompt and appends a `## Data sources` block describing each bound `DataSource`
- `bros/` — one file per specialised agent; `bros/__init__.py:init()` registers each with the registry, wiring `flow.mcp.bridge.create_flow_server()` and/or data sources as tool sources. Currently `assistant.py` (general-purpose) and `pm.py` (Flow inbox triage)
- `datasources/` — `DataSource` ABC + connectors to read-only sources (books, databases, web references). Each connector exposes `<name>-search` / `<name>-fetch` tools via `as_mcp_server()`. Currently `wikipedia.py` (Wikipedia REST API, query-aware fetch via `mu`)
- `registry.py` — process-wide registry (`register`, `get_bro`, `list_bros`); first lookup triggers `bros.init()`
- `run.py` (`bro`) — CLI: `bro run <name> --input ...` and `bro list`
- `server/server.py` (`bro.server.server`) — aiohttp wrapper exposing the `assistant` Bro on `POST /v1/chat/completions` (OpenAI-compatible). Used by the iOS app
- `app/ios/` — iOS chat client; see `app/ios/CLAUDE.md`

## Adding a Bro

Create `bros/<name>.py` with a `Bro` subclass (`name`, `description`, `system_prompt`), then add a `register(YourBro, mcp_servers=..., data_sources=...)` line in `bros/__init__.py:init()`. The flow tools come from `flow.mcp.bridge.create_flow_server()`. Data sources come from `bro/datasources/`.

## Adding a DataSource

Subclass `bro.datasources.base.DataSource`, set `name` (slug) and `summary` (one-line; injected into the system prompt of every Bro that uses it), and implement `async search(query, limit) -> list[Hit]` and `async fetch(id, query=None) -> str`. `fetch` receives the original user query so the connector can return a focused summary (see `wikipedia.py` for the `mu`-based pattern). Register on a Bro via `register(YourBro, data_sources=[YourSource()])`.
