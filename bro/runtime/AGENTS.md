# Serving processes

Fronts that compose lower-level packages into runnable services.

## Components

- `mcp_server.py` — generic stdio or HTTP MCP server.
  Plain names resolve `bro.toolsets` entries targeting a `Toolset`;
  `<prefix>:<value>` resolves the matching `bro.mcp.targets` entry, whose callable returns an assembled list of live servers.
  HTTP exposes one endpoint per namespace.
  The HTTP lifespan closes every resolved server after its session managers, and stdio closes its server after the streams end;
  eager tool-listing or endpoint-construction failures close them before propagating.
