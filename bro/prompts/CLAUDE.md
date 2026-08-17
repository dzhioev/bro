# bro/prompts/CLAUDE.md

Centralised prompt store. Five loading conventions: auto-inject into every bro + `cw ss` session (`shared/`); serve as a reference doc — injected into `cw ss` sessions or mounted as a `FileSource` tool (`*.md`, top level or a subdirectory like `dev/`); inject the hold fragment at launch (`hold.md` composing `holds/`); splice into an opting-in text via `{{include}}` (`fragments/`); load explicitly by name (top-level `*.prompt` / `*.prompt.template`).

## Files

- `*.prompt` — plain text, used as-is. Loaded with `bro.prompts.get_prompt('name.prompt')`
- `*.prompt.template` — Python `str.format` template. Requires kwargs at load time; `get_prompt` enforces "template ↔ kwargs" symmetry — passing kwargs to a non-template, or omitting kwargs for a template, raises

Prompt content may carry `bro.base.template` directives (`#harness`/`#wire`/`#creds`; grammar and semantics: `bro/reference/template.md`): every rendering surface renders its text once with its own facts via `bro.llm.mcp.render_text` — `BaseBro.__init__` for the two bro flavors, `ride/ride/claude/system_prompt.py:_session_append_prompt` for cw-sessions — so a directive works in `shared/` and bro class prompts alike. `FileSource`-served docs are the exception: one rendering is read by every harness, so their bodies must be surface-neutral — `FileSource.read` supplies no facts and a surface directive raises.

Harness-specific conditioning is expressed with these directives, never as prose that addresses both surfaces and leaves the reader to pick: fork the text with `{{iff #harness = bro}}…{{eliff #harness = claude}}…{{end}}` — the chain raises when no branch matches, so the fork is self-guarding — and each surface reads only its own instruction.

`PromptLoader` is the contained directory-backed loader; `__init__.py` binds the framework's module-level `get_prompt` / `get_prompt_path` surface to `bro/prompts/`. Consumer packages bind their own loader for package-local prompts. Do not `open()` prompts ad-hoc from elsewhere. Names are contained to the loader's directory: a name that resolves outside it (`..` traversal, absolute path) raises. `get_prompt_path(name)` returns the `Path` if a caller needs to hand the file off to something that wants a path rather than the body (e.g. `bro.datasources.file.FileSource`). `{{include <name>}}` directives resolve through it too — `bro.llm.mcp.render_text` wires `get_prompt` as the template engine's include resolver, so a spliced prompt loads exactly like a directly-requested one.

## Auto-injected `shared/` directory

`bro/prompts/shared/*.md` is appended to the system prompt of every Bro (see `bro/bro.py:_load_shared_prompts`) AND injected into every `cw ss` Claude Code session via `ride/ride/claude/system_prompt.py:_load_base_prompts`. Conventions that must hold across both surfaces and at every hold (e.g. word choices) belong here. Files are sorted alphabetically at load time, so prefix with `00-`, `10-`, etc. if order matters.

## `*.md` reference docs

A markdown reference doc in `bro/prompts/` — top level, or a non-`shared/` subdirectory like `dev/` (loader names are `/`-relative) — reaches sessions one of two ways:

- **injected** — listed in `ride/ride/claude/system_prompt.py:_BASE_PROMPT_FILES`, appended to every `cw ss` session's `--append-system-prompt`. For content every cw-session must carry unconditionally.
- **tool-served** — declared as a `FileSource` in `bro/datasources/references.py`, then mounted in a bro's `data_sources` either on its own (a `read` tool of its own namespace) or as one topic of the `man` roster there: on every harness the bro serves, and the framework lists whichever it mounts in the bro system prompt's `## Data sources` block. For reference docs the agent consults on demand; the body must be surface-neutral (no `#harness` forks — `FileSource.read` renders with no facts and raises on one).

Current reference docs:

- `environment.md` — session-banner playbook: every surface calls the `bro::banner` service tool and reads this doc through the `environment` page (`cw banner --llm` stays as the human CLI). Tool-served only — not injected

- `dev/style.md` — the development style policy, tool-served through the `dev-style` `FileSource` mounted on the Dev bro (the persona directs a read at session start and re-reads on demand — e.g. [[run pr]]'s policy audit before each commit's verdict). Not injected
- `tool_names.md` — the tool-name resolution rule, templated on the `#wire` scheme; one file serves every surface. Claude sessions get the `mcp` rendering (`ns::tool` → `mcp__ns__tool`): injected here for non-raw sessions, composed into `BaseBro.claude_system_prompt` for `cw ss --raw` ones. Bro-native LLM runs compose the `bare` rendering (`ns::tool` → `ns__tool`) into `BaseBro.system_prompt`. Deliberately no `FileSource`

## Hold text

A session's hold — its user-involvement level — is one of `unattended | detached | attended | guided`, ordered from no human channel to human-driven. Every session gets exactly one level's text, picked by the launching surface at session start — a session is told its hold, never left to detect it at runtime:

- `cw ss` picks by its `--hold` flag for both claude flavors — the cw-session append prompt and the `--raw` `--system-prompt` (flag semantics: `bro/reference/cw.md`)
- the bro-native launch surfaces pick it through `bro/bro.py:_system_prompt_for` — `run()` defaults unattended, `send()` guided, with every launcher's `--hold` overriding (per-surface defaults: `bro/launch/CLAUDE.md`, "Launch holds")

`hold.md` (top level, not in `_BASE_PROMPT_FILES`) selects the per-level file in `holds/` via an exhaustive `{{iff #hold = …}}` chain and `{{include}}`s it; the three non-guided level files share `holds/authorization.md`, the full-authorization block, and the three interactive levels — detached, attended, guided — share `fragments/interaction.md`, the interaction policy. `bro.prompts.hold_fragment(hold, …facts)` is the one rendering path — every injection site uses it (`ride/ride/claude/system_prompt.py:_session_append_prompt`, `ride/ride/claude/claude_argv.py` for `--raw`, `bro/bro.py:_system_prompt_for`), and it is the only call that supplies the `#hold` fact, so all other text stays hold-neutral mechanically: a stray `#hold` directive in a spell or procedure doc raises.

The level files are the single place the levels differ: unattended carries the never-ask + `raise` convention, detached the carry-questions-into-the-report convention, attended the end-the-turn-at-pivotal-points convention, guided the confirm-each-significant-step convention.

## Bare-session grounding fragment

`grounding.md` (top level, not in `_BASE_PROMPT_FILES`) is the tool-grounding rule for `cw ss --raw` sessions: `BaseBro` appends it at the end of both composed bro prompt flavors — last, where instruction recency is strongest — and the file's own directives render its body only for the claude-bare surface (harness `bro`, wire `mcp`), the flavor whose argv-seeded first turn can reach the model before its MCP servers connect (`bro/reference/cw.md` "Session-local MCP serving").

## Top-level one-shot prompts

`*.prompt` / `*.prompt.template` files at the top level are framework one-shot prompts loaded by name from their callers.

## Adding a prompt

- **One-shot**: drop `<name>.prompt` (or `<name>.prompt.template` for `str.format` slots) at the top level. Load with `get_prompt('<name>.prompt'[, **kwargs])`
- **Auto-injected into bros and `cw ss` sessions**: drop a `*.md` in `shared/`. Conventions that must hold for both surfaces at every hold (word choices, tone) belong here
- **Include fragment**: drop a `*.md` in `fragments/` and splice it with `{{include fragments/<name>.md}}` from each opting-in text. For a convention that applies only where a capability exists — e.g. `task_tracker.md`, included by every persona that mounts task-tracker tools rather than injected everywhere — or only at some holds, e.g. `interaction.md`, included by the interactive level files
- **Reference doc**: drop a `*.md` at top level or in a subdirectory (e.g. `dev/style.md`), then either add its filename to `ride/ride/claude/system_prompt.py:_BASE_PROMPT_FILES` (injected into every `cw ss` session) or declare a `FileSource` for it in `bro/datasources/references.py` — mounted on a bro directly or joining the `man` roster there (tool-served on demand, every harness — e.g. `environment.md`)
