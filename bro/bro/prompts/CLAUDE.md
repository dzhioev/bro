# prompts/CLAUDE.md

Centralised prompt store. Four loading conventions: auto-inject into every bro + `cw ss` session (`shared/`); serve as a reference doc — injected into `cw ss` sessions or mounted as a `FileSource` tool (top-level `*.md`); inject one of a pair per session mode at launch (the `autonomous_session.md` / `manual_session.md` fragments); load explicitly by name (top-level `*.prompt` / `*.prompt.template`).

## Files

- `*.prompt` — plain text, used as-is. Loaded with `prompts.get_prompt('name.prompt')`
- `*.prompt.template` — Python `str.format` template. Requires kwargs at load time; `get_prompt` enforces "template ↔ kwargs" symmetry — passing kwargs to a non-template, or omitting kwargs for a template, raises

Prompt content may carry `base.template` directives (`#harness`/`#wire`/`#creds`; grammar and semantics: `reference/template.md`): every rendering surface renders its text once with its own facts via `llm.mcp.render_text` — `BaseBro.__init__` for the two bro flavors, `cw/system_prompt.py:_session_append_prompt` for cw-sessions — so a directive works in `shared/` and bro class prompts alike. `FileSource`-served docs are the exception: one rendering is read by every harness, so their bodies must be surface-neutral — `FileSource.read` supplies no facts and a surface directive raises.

Harness-specific conditioning is expressed with these directives, never as prose that addresses both surfaces and leaves the reader to pick: fork the text with `{{iff #harness = bro}}…{{eliff #harness = claude}}…{{end}}` — the chain raises when no branch matches, so the fork is self-guarding — and each surface reads only its own instruction (see the ppp-dev persona prompt in `bro/bros/ppp_dev/__init__.py` for the pattern).

`prompts.py` (repo root) is the loader. Keep it the single entry point — do not `open()` prompts ad-hoc from elsewhere. `get_prompt_path(name)` returns the `Path` if a caller needs to hand the file off to something that wants a path rather than the body (e.g. `bro.datasources.file.FileSource`).

## Auto-injected `shared/` directory

`prompts/shared/*.md` is appended to the system prompt of every Bro (see `bro/bro.py:_load_shared_prompts`) AND injected into every `cw ss` Claude Code session via `cw/system_prompt.py:_load_base_prompts`. Conventions that must hold across both surfaces (e.g. interaction policy) belong here. Files are sorted alphabetically at load time, so prefix with `00-`, `10-`, etc. if order matters.

## Top-level `*.md` reference docs

A top-level markdown file in `prompts/` reaches sessions one of two ways:

- **injected** — listed in `cw/system_prompt.py:_BASE_PROMPT_FILES`, appended to every `cw ss` session's `--append-system-prompt`. For content every cw-session must carry unconditionally.
- **tool-served** — declared as a `FileSource` in a bro's `data_sources` (`bro/datasources/references.py` holds the ready-made instances): the source mounts a `read` tool on every harness the bro serves, and the framework lists it in the bro system prompt's `## Data sources` block. For reference docs the agent consults on demand; the body must be surface-neutral (no `#harness` forks — `FileSource.read` renders with no facts and raises on one).

Current top-level reference docs:

- `environment.md` — session-banner playbook: every surface calls the `bro::banner` service tool and reads this doc through the `environment` `FileSource` (`cw banner --llm` stays as the human CLI). Tool-served only — not injected
- `tool_names.md` — the tool-name resolution rule, templated on the `#wire` scheme; one file serves every surface. Claude sessions get the `mcp` rendering (`ns::tool` → `mcp__ns__tool`): injected here for non-bro sessions, composed into `BaseBro.claude_system_prompt` for `cw ss --bro` ones. Bro-native LLM runs compose the `bare` rendering (`ns::tool` → `ns__tool`) into `BaseBro.system_prompt`. Deliberately no `FileSource`

## Session-mode fragments

`autonomous_session.md` / `manual_session.md` (top level, not in `_BASE_PROMPT_FILES`) are a pair of which every session gets exactly one, picked by the launching surface at session start — a session is told its mode, never left to detect it at runtime:

- `cw/system_prompt.py:_mode_prompt` picks by the `--auto` flag for both claude flavors (the cw-session append prompt and the `--bro` `--system-prompt`), adding the `Land mode: PR` line to the autonomous side
- `bro/bro.py:_system_prompt_for` picks by run kind for bro-native LLM runs — non-interactive → autonomous, interactive → manual

The autonomous fragment carries the authorization convention (the initial request authorizes the actions it entails; ambiguous scope aborts); the manual fragment carries the confirmation convention (summarize + ask before each significant step). Skills and procedure docs stay mode-neutral — these fragments are the single place the modes differ.

## Top-level one-shot prompts

`*.prompt` / `*.prompt.template` files at the top level are explicit one-shot prompts loaded by name from their callers (e.g. `email_to_markdown.prompt`).

## Adding a prompt

- **One-shot**: drop `<name>.prompt` (or `<name>.prompt.template` for `str.format` slots) at the top level. Load with `get_prompt('<name>.prompt'[, **kwargs])`
- **Auto-injected into bros and `cw ss` sessions**: drop a `*.md` in `shared/`. Conventions that must hold for both surfaces (interaction policy, tone) belong here
- **Reference doc**: drop a `*.md` at top level, then either add its filename to `cw/system_prompt.py:_BASE_PROMPT_FILES` (injected into every `cw ss` session) or declare a `FileSource` for it in `bro/datasources/references.py` and mount it on a bro (tool-served on demand, every harness — e.g. `environment.md`)
