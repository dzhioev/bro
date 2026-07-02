# Tool names

`namespace::tool` is a tool's canonical, harness-agnostic name (e.g. `flow::get_task_info`, `wikipedia-source::search`); it can appear anywhere — a skill, a doc, a tool description, a user message. In a Claude Code session it resolves to the MCP tool `mcp__namespace__tool`: replace `::` with `__` and prepend `mcp__`.
