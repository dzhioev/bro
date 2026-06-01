# prompts/CLAUDE.md

Centralised prompt store. Three loading conventions: auto-inject into every bro + `cw ss` session (`shared/`); auto-inject into `cw ss` sessions only and expose to specific bros on demand (top-level `*.md`); load explicitly by name (top-level `*.prompt` / `*.prompt.template`).

## Files

- `*.prompt` — plain text, used as-is. Loaded with `prompts.get_prompt('name.prompt')`
- `*.prompt.template` — Python `str.format` template. Requires kwargs at load time; `get_prompt` enforces "template ↔ kwargs" symmetry — passing kwargs to a non-template, or omitting kwargs for a template, raises

`prompts.py` (repo root) is the loader. Keep it the single entry point — do not `open()` prompts ad-hoc from elsewhere. `get_prompt_path(name)` returns the `Path` if a caller needs to hand the file off to something that wants a path rather than the body (e.g. `bro.datasources.file.FileSource`).

## Auto-injected `shared/` directory

`prompts/shared/*.md` is appended to the system prompt of every Bro (see `bro/bro.py:_load_shared_prompts`) AND injected into every `cw ss` Claude Code session via `cw.py:_load_base_prompts`. Conventions that must hold across both surfaces (e.g. interaction policy) belong here. Files are sorted alphabetically at load time, so prefix with `00-`, `10-`, etc. if order matters.

## Top-level `*.md` reference docs

Top-level markdown files in `prompts/` are dual-purpose reference docs:

- Auto-injected into every `cw ss` Claude Code session via `cw.py:_load_base_prompts` (the file name must be listed in `_BASE_PROMPT_FILES`)
- Available to specific bros via a `FileSource` declaration in `data_sources` (see `bro/bros/ppp_dev/__init__.py` for the `environment.md` example). The bro framework auto-lists the source in the bro's `## Data sources` block and mounts a `<source>-read` tool that returns the file body.

Use this when the same playbook must hold in both a Claude Code session prompt and a specific bro — but doesn't make sense to push onto bros that won't use it (which is what `shared/` would do).

Current top-level reference docs:

- `environment.md` — `cw banner` playbook; loaded into every `cw ss` session and exposed to `ppp-dev` via `FileSource('environment', ...)`

## Top-level one-shot prompts

`*.prompt` / `*.prompt.template` files at the top level are explicit one-shot prompts loaded by name from their callers (e.g. `email_to_markdown.prompt`).

## Adding a prompt

- **One-shot**: drop `<name>.prompt` (or `<name>.prompt.template` for `str.format` slots) at the top level. Load with `get_prompt('<name>.prompt'[, **kwargs])`
- **Auto-injected into bros and `cw ss` sessions**: drop a `*.md` in `shared/`. Conventions that must hold for both surfaces (interaction policy, tone) belong here
- **Auto-injected into `cw ss` sessions + on-demand for a specific bro**: drop a `*.md` at top level, add its filename to `cw.py:_BASE_PROMPT_FILES`, and declare a `FileSource` on the bro that should see it
