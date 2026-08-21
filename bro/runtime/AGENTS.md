# Serving processes

Fronts that compose lower-level packages into runnable services.

## Components

- `mcp_server.py` — generic stdio or HTTP MCP server.
  Plain names resolve `bro.toolsets` entries targeting a `Toolset`;
  `<prefix>:<value>` resolves the matching `bro.mcp.targets` entry, whose callable returns an assembled list of live servers.
  HTTP exposes one endpoint per namespace.
