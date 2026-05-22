# prompts/CLAUDE.md

Centralised prompt store. Two file conventions, two injection conventions.

## Files

- `*.prompt` — plain text, used as-is. Loaded with `prompts.get_prompt('name.prompt')`
- `*.prompt.template` — Python `str.format` template. Requires kwargs at load time; `get_prompt` enforces "template ↔ kwargs" symmetry — passing kwargs to a non-template, or omitting kwargs for a template, raises

`prompts.py` (repo root) is the loader. Keep it the single entry point — do not `open()` prompts ad-hoc from elsewhere.

## Auto-injected directories

- `prompts/shared/` — appended to the system prompt of every Bro (see `bro/bro.py:_load_shared_prompts`) AND injected into every `cw ss` Claude Code session. Conventions that must hold across both surfaces (e.g. interaction policy) belong here
- `prompts/base/` — injected into every `cw ss` Claude Code session only. Claude-Code-specific defaults (environment detection, etc.)

Both directories are loaded by `cw.py:_load_base_prompts` (sorted glob, concatenated with blank lines). Bros load only `shared/`.

Top-level `.prompt` / `.prompt.template` files are explicit one-shot prompts loaded by name from their callers (e.g. `email_to_markdown.prompt`, `flow_bundle.prompt.template`).

## Adding a prompt

- **One-shot**: drop `<name>.prompt` (or `<name>.prompt.template` for `str.format` slots) at the top level of `prompts/` and load it where you need it with `get_prompt('<name>.prompt'[, **kwargs])`. `prompts.py` is the only loader — do not `open()` prompt files directly
- **Auto-injected into Bros and `cw ss` sessions**: drop a `*.md` in `shared/`. Conventions that must hold for both surfaces (interaction policy, tone) belong here
- **Auto-injected into `cw ss` sessions only**: drop a `*.md` in `base/`. Claude-Code-specific defaults
- Filenames are alphabetised at load time (sorted glob), so prefix with `00-`, `10-`, etc. if order matters
