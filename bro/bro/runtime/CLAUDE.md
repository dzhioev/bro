# runtime/CLAUDE.md

Serving-process fronts that compose lower-level packages into runnable services.

## Components

- `mcp_server.py` — generic stdio or HTTP MCP server that resolves and combines configured tool servers
