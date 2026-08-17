# Template directives

Conditional rendering for static agent-facing text — system prompts, spell bodies, tool descriptions. `bro/base/template.py` parses `{{…}}` directive groups and lowers their conditions onto the declarative conditioning model (`bro/reference/conditions.md`), which owns the condition semantics; this file owns the text syntax and where text renders.

## Grammar

```
template  := (text | block | 'when'-block | '{{assert' condition '}}'
              | '{{include' file '}}' | '{{insert' '#'name '}}')*
block     := '{{iff' condition '}}' template
             ('{{eliff' condition '}}' template)*
             ('{{else}}' template)?
             '{{end}}'
when-block:= '{{when' condition '}}' template '{{end}}'
condition := value ('=' value | 'contains' value)
value     := '#' name | name            name: [A-Za-z0-9_-]+
file      := prompt file name           file: [A-Za-z0-9._/-]+
```

## Semantics

- a `#name` value references a variable supplied by the rendering surface; a bare name is a string literal

- `=` lowers onto the model's equality and `contains` onto its membership (container first: `#creds contains openai`) — evaluation, typing, and the fail-fast rules are the condition model's (`bro/reference/conditions.md`); a violation surfaces as `TemplateError`

- `{{when c}}…{{end}}` is optional inclusion: the body renders when the condition holds and disappears otherwise — absence is meaningful, never an error. the text mirror of the code front's `when(c, item)`. a `when` block has no `eliff`/`else`

- `{{iff c1}}…{{eliff c2}}…{{else}}…{{end}}` is exhaustive choice, the text mirror of `iff(c1, a1, c2, a2[, e])`: the first holding branch renders; without an `{{else}}`, a chain none of whose branches match raises — the implicit exhaustiveness guard (`{{iff #harness = bro}}…{{eliff #harness = claude}}…{{end}}` needs no trailing assert). the guard fires only when the chain is in emitted text; a chain inside a skipped branch stays silent

- `{{assert}}` renders to nothing when its condition holds and raises when it does not — the standalone precondition for text that must only ever render under a known state

- `{{include <file>}}` splices another prompt file into the output, rendered recursively against the includer's own variables — same facts, same fail-fast semantics. resolution goes through the rendering surface's resolver (`render` takes an `include_resolver` callback; `render_text` wires the `prompts` loader); a render with no resolver raises on any include it parses, and an include chain that revisits a file raises

- `{{insert #name}}` emits the referenced string variable's value — substitution for text parameterized by a fact (e.g. a data source's own name in its tool descriptions). only a string variable has a text form: referencing a set or boolean variable raises, as does an unknown name — even in a non-taken branch, mirroring condition evaluation

- conditions in non-taken branches are still evaluated (a typo fails every render, not just the unlucky branch), while `{{assert}}` directives and fall-through guards in non-taken branches do not fire; blocks nest

- an include in a non-taken branch follows the same rule: the file is loaded and structurally parsed — a broken name or malformed file fails every render — but it is not emitted and directives inside it do not evaluate (its facts may be foreign to this surface)

- only `{{` groups whose first token is a directive keyword (`when` / `iff` / `eliff` / `else` / `end` / `assert` / `include` / `insert`) are parsed; any other `{{…}}` is literal text, so braces in code samples survive rendering

- `true` and `false` are built-in boolean variables

## Rendering surfaces

`bro.llm.mcp.render_text(text, harness=…, wire=…, creds=…, hold=…, extra=…)` renders directives against the facts the call site knows (the facts, `#hold`'s single-purpose supply rule included, are documented in `bro/reference/conditions.md`; `extra` merges a caller-owned vocabulary next to them — the bro surfaces pass the owning bro's `#features`) and resolves `{{include}}` targets through the `prompts` loader. Each surface renders its copy once, with its own facts:

- `BaseBro.__init__` — the two bro prompt flavors (harness `bro`; wire `bare` / `mcp`)
- `ride/ride/claude/system_prompt.py` — a managed Claude session's append prompt, the injected persona included (harness `claude`, wire `mcp`)
- `bro.prompts.hold_fragment` — the hold text (`bro/prompts/hold.md` selecting over `bro/prompts/holds/`), the only surface that supplies `#hold`
- spell bodies — each `spell::` tool renders for its serving harness; bro-native and `--raw` use the bro branch, while a cw persona session uses the Claude branch
- tool descriptions and parameter annotations — rendered by the owning server at build time against its own vocabulary, not the harness facts (`#tools` for a `Toolset`'s roster, a data source's `#features` + `#source`; the bro service-tool build additionally injects `#wire`), so no unprocessed directive leaves a server and a standalone server serves final text — see `bro/reference/conditions.md` "Server-domain vocabularies"
- data-source summaries — `DataSource.rendered_summary()`, the source's vocabulary again, rendered where the prompt composes
- credential install hooks — `bro.base.credentials.Secret.from_dict` renders each registry secret's `install` text with `#name` bound to the secret's own name, its own single-variable vocabulary like the server-domain ones
- `FileSource.read` — no facts: one rendering is read by every harness, so a served doc must be surface-neutral and a `#harness`/`#wire`/`#creds` directive raises; `render=False` opts a source out entirely, for a doc whose payload is the directive syntax itself (this reference and `bro/reference/conditions.md`)

Authoring rule for prompt files — fork with directives rather than writing dual-surface prose — lives in `bro/prompts/AGENTS.md`.
