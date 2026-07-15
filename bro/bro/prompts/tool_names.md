# Tool names

`namespace::tool` is a tool's canonical, harness-agnostic name (e.g. `bro::banner`); it can appear anywhere — a skill, a doc, a tool description, a user message. {{iff #wire = bare}}In your tool list it is `namespace__tool`: replace `::` with `__` and call that wire name directly.{{eliff #wire = mcp}}It resolves to the MCP tool `mcp__namespace__tool`: replace `::` with `__` and prepend `mcp__`.{{end}}
