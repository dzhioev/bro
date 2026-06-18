# Tool names

Skills and shared docs name a tool by its canonical, harness-agnostic form `namespace::tool` (e.g. `flow::get_task_info`, `wikipedia-source::search`). In a Claude Code session that is the MCP tool `mcp__namespace__tool`: replace `::` with `__` and prepend `mcp__`. If it isn't already loaded, fetch it deterministically with `ToolSearch select:mcp__namespace__tool` rather than a fuzzy keyword search — the canonical `::` name never appears on the wire, so a fuzzy search misses.
