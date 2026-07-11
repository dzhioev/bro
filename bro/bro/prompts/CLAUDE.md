# prompts/CLAUDE.md

Centralised prompt store. Four loading conventions: auto-inject into every bro + `cw ss` session (`shared/`); auto-inject into `cw ss` sessions only and expose to specific bros on demand (top-level `*.md`); inject one of a pair per session mode at launch (the `autonomous_session.md` / `manual_session.md` fragments); load explicitly by name (top-level `*.prompt` / `*.prompt.template`).

## Files

- `*.prompt` — plain text, used as-is. Loaded with `prompts.get_prompt('name.prompt')`
- `*.prompt.template` — Python `str.format` template. Requires kwargs at load time; `get_prompt` enforces "template ↔ kwargs" symmetry — passing kwargs to a non-template, or omitting kwargs for a template, raises

Prompt content may carry `base.template` directives (`#harness`/`#wire`/`#creds`): every composed surface renders its assembled text once with its own facts via `llm.mcp.render_text` — `BaseBro.__init__` for the two bro flavors, `cw/system_prompt.py:_session_append_prompt` for native claude sessions — so a directive works in `shared/`, top-level files, and bro class prompts alike.

`prompts.py` (repo root) is the loader. Keep it the single entry point — do not `open()` prompts ad-hoc from elsewhere. `get_prompt_path(name)` returns the `Path` if a caller needs to hand the file off to something that wants a path rather than the body (e.g. `bro.datasources.file.FileSource`).

## Auto-injected `shared/` directory

`prompts/shared/*.md` is appended to the system prompt of every Bro (see `bro/bro.py:_load_shared_prompts`) AND injected into every `cw ss` Claude Code session via `cw/system_prompt.py:_load_base_prompts`. Conventions that must hold across both surfaces (e.g. interaction policy) belong here. Files are sorted alphabetically at load time, so prefix with `00-`, `10-`, etc. if order matters.

## Top-level `*.md` reference docs

Top-level markdown files in `prompts/` are auto-injected into every `cw ss` Claude Code session via `cw/system_prompt.py:_load_base_prompts` (the file name must be listed in `_BASE_PROMPT_FILES`). Each can optionally also be exposed to a specific bro via a `FileSource` declaration in `data_sources` (see `bro/bros/ppp_dev/__init__.py` for the `environment.md` example — the bro framework auto-lists the source in the `## Data sources` block and mounts a `read` tool that returns the file body).

Use a top-level file when content must hold in a `cw ss` Claude Code session — optionally shared with a specific bro via `FileSource`, but **not** pushed onto every bro the way `shared/` would. A file is Claude-Code-only precisely when no bro declares a `FileSource` for it.

Current top-level reference docs:

- `environment.md` — `cw banner` playbook; loaded into every `cw ss` session and exposed to `ppp-dev` via `FileSource('environment', ...)`
- `tool_names.md` — the tool-name resolution rule, templated on the `#wire` scheme; one file serves every surface. Claude sessions get the `mcp` rendering (`ns::tool` → `mcp__ns__tool`): injected here for non-bro sessions, composed into `BaseBro.claude_system_prompt` for `cw ss --bro` ones. Bro-native LLM runs compose the `bare` rendering (`ns::tool` → `ns__tool`) into `BaseBro.system_prompt`. Deliberately no `FileSource`

## Session-mode fragments

`autonomous_session.md` / `manual_session.md` (top level, not in `_BASE_PROMPT_FILES`) are a pair of which every session gets exactly one, picked by the launching surface at session start — a session is told its mode, never left to detect it at runtime:

- `cw/system_prompt.py:_mode_prompt` picks by the `--auto` flag for both claude flavors (native append prompt and the `--bro` `--system-prompt`), adding the `Land mode: PR` line to the autonomous side
- `bro/bro.py:_system_prompt_for` picks by run kind for bro-native LLM runs — non-interactive → autonomous, interactive → manual

The autonomous fragment carries the authorization convention (the initial request authorizes the actions it entails; ambiguous scope aborts); the manual fragment carries the confirmation convention (summarize + ask before each significant step). Skills and procedure docs stay mode-neutral — these fragments are the single place the modes differ.

## Top-level one-shot prompts

`*.prompt` / `*.prompt.template` files at the top level are explicit one-shot prompts loaded by name from their callers (e.g. `email_to_markdown.prompt`).

## Adding a prompt

- **One-shot**: drop `<name>.prompt` (or `<name>.prompt.template` for `str.format` slots) at the top level. Load with `get_prompt('<name>.prompt'[, **kwargs])`
- **Auto-injected into bros and `cw ss` sessions**: drop a `*.md` in `shared/`. Conventions that must hold for both surfaces (interaction policy, tone) belong here
- **Auto-injected into `cw ss` Claude Code sessions only**: drop a `*.md` at top level and add its filename to `cw/system_prompt.py:_BASE_PROMPT_FILES`. Leave it without a `FileSource` to keep it Claude-Code-only; add a `FileSource` on a bro to also expose it there on demand (e.g. `environment.md`)
