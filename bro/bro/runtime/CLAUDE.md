# runtime/CLAUDE.md

Serving-process fronts that compose lower-level packages into runnable services.

## Components

- `mcp_server.py` — generic stdio or HTTP MCP server that resolves `bro.toolsets` entry points by namespace and combines configured tool servers; `bro:<name>` / `persona:<name>` resolve agent-composed surfaces
