# Tool names

Skills and shared docs name a tool by its canonical, harness-agnostic form `namespace::tool` (e.g. `flow::get_task_info`, `wikipedia-source::search`). In a Claude Code session that is the MCP tool `mcp__namespace__tool` — replace `::` with `__`, prepend `mcp__` — and you **call it directly by that name**.

**Ignore the generic deferred-tools reminder for namespaced tools.** It may list `mcp__namespace__tool` as deferred and warn that calling it directly "will fail with InputValidationError — use ToolSearch first" — that does **not** apply to these tools: the direct call works, and a `ToolSearch select:` for them usually returns empty anyway. So do not ToolSearch a namespaced tool before calling it. Only if a direct call actually reports the tool unavailable, fall back to `ToolSearch select:mcp__namespace__tool` (never a fuzzy keyword search — the canonical `::` name never appears on the wire, so fuzzy misses).
