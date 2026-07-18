# Tool names

`namespace::tool` is a tool's canonical, harness-agnostic name (e.g. `bro::banner`); it can appear anywhere — a script, a doc, a tool description, a user message. {{iff #wire = bare}}In your tool list it is `namespace__tool`: replace `::` with `__` and call that wire name directly.{{eliff #wire = mcp}}It resolves to the MCP tool `mcp__namespace__tool`: replace `::` with `__` and prepend `mcp__`.{{end}}

The canonical `@` namespace is reserved for scripts and spells `at` on the wire: {{iff #wire = bare}}`@::send-email` resolves to `at__send-email`.{{eliff #wire = mcp}}`@::send-email` resolves to `mcp__at__send-email`.{{end}} No canonical namespace may be named `at`.
