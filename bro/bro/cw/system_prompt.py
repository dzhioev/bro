from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'
# auto-injected into every `cw ss` session via --append-system-prompt. Files in
# `shared/` also flow into every bro (via bro/bro.py:_load_shared_prompts), so
# put cross-surface conventions there. Prompt files may carry template
# directives (`#harness`/`#wire`/`#creds`) — the whole append text renders once
# in `_session_append_prompt` with this surface's facts. The one top-level file
# injected here is `tool_names.md` — the tool-name resolution rule, templated
# on `#wire`; `--raw` sessions run `--bare` and skip this injection but get the
# same file through `BaseBro.claude_system_prompt`, and bro-native LLM runs
# compose its bare rendering — do not give a bro a `FileSource` for this file.
# Other reference docs (`environment.md`, …) reach every harness as `FileSource`
# tools instead (bro/datasources/references.py).
_BASE_PROMPT_DIRS = ['shared']
_BASE_PROMPT_FILES = ['tool_names.md']


def _load_base_prompts() -> str:
  parts = []
  for subdir in _BASE_PROMPT_DIRS:
    for p in sorted((_PROMPTS_DIR / subdir).glob('*')):
      if p.is_file():
        parts.append(p.read_text().strip())
  for name in _BASE_PROMPT_FILES:
    p = _PROMPTS_DIR / name
    if p.is_file():
      parts.append(p.read_text().strip())
  return '\n\n'.join(parts)


def _session_append_prompt(hold: str, bro_name: str) -> str:
  """--append-system-prompt text for a cw-session (the non --raw flavor).

  base prompts plus the session bro's own persona prompts and script
  instructions (`bro_name`, the `--bro` bro) — so a cw-session carries the
  bro's policies even though it runs the Claude Code harness. the assembled text renders
  once with this surface's facts: the claude harness over mcp wire names, with
  the session environment's credentials (this composes in the session's own
  process — in-container for container sessions — so the store is the scoped
  one). the hold fragment renders separately through
  `prompts.hold_fragment` — the `#hold` fact is supplied only there, so the
  base and persona prompts stay hold-neutral.
  """
  # llm.mcp and bro.registry are imported lazily — the hub aggregates every cw
  # submodule, so a module-level import here would tax every `import cw`.
  import llm.mcp
  import prompts
  from base import credentials
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  parts = [_load_base_prompts(), bro.persona]
  script_instructions = bro.script_instructions()
  if len(script_instructions) > 0:
    parts.append(script_instructions)
  rendered = llm.mcp.render_text(
    '\n\n'.join(parts), harness='claude', wire='mcp', creds=credentials.known_names()
  )
  fragment = prompts.hold_fragment(
    hold, harness='claude', wire='mcp', creds=credentials.known_names()
  )
  return f'{rendered}\n\n{fragment}'
