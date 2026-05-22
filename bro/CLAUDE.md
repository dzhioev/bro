# bro/CLAUDE.md

Bro is the agent system: independent specialised agents (a "Bro") each run as a stateless LLM loop with their own system prompt and MCP-tool access. Conceptual model and rationale: `DESIGN.md`. Run any script with `--help` for flags.

## Layout

- `bro.py` — `Bro` ABC. Subclasses set `name`, `description`, `system_prompt`; instances are wired with MCP servers at registration time. `Bro` auto-prepends every `prompts/shared/*.md` to the system prompt
- `bros/` — one file per specialised agent; `bros/__init__.py:init()` registers each with the registry, wiring `flow.mcp.bridge.create_flow_server()` as its tool source. Currently `assistant.py` (general-purpose) and `pm.py` (Flow inbox triage)
- `registry.py` — process-wide registry (`register`, `get_bro`, `list_bros`); first lookup triggers `bros.init()`
- `run.py` (`bro`) — CLI: `bro run <name> --input ...` and `bro list`
- `server/server.py` (`bro.server.server`) — aiohttp wrapper exposing the `assistant` Bro on `POST /v1/chat/completions` (OpenAI-compatible). Used by the iOS app
- `app/ios/` — iOS chat client; see `app/ios/CLAUDE.md`

## Adding a Bro

Create `bros/<name>.py` with a `Bro` subclass (`name`, `description`, `system_prompt`), then add a `register(YourBro, mcp_servers=...)` line in `bros/__init__.py:init()`. The flow tools come from `flow.mcp.bridge.create_flow_server()`.
