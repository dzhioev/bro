from pathlib import Path

import bro.prompts as prompts

_PROMPTS_DIR = Path(prompts.__file__).parent
# auto-injected into every `ride solo|along` session via --append-system-prompt. Files in
# `shared/` also flow into every bro (via bro/bro.py:_load_shared_prompts), so
# put cross-surface conventions there. Prompt files may carry template
# directives (`#harness`/`#wire`/`#creds`, plus the bro's own `#features`) —
# the whole append text renders once in `session_append_prompt` with this
# surface's facts and the session bro's vocabulary. The one top-level file
# injected here is `tool_names.md` — the tool-name resolution rule, templated
# on `#wire`; `--raw` sessions run `--bare` and skip this injection but get the
# same file through `BaseBro.claude_system_prompt`, and bro-native LLM runs
# compose its bare rendering — do not give a bro a `FileSource` for this file.
# Other reference docs (`environment.md`, …) reach every harness as `FileSource`
# tools instead (bro/datasources/references.py).
_BASE_PROMPT_DIRECTORIES = ['shared']
_BASE_PROMPT_FILES = ['tool_names.md']


def _load_base_prompts() -> str:
  parts = []
  for directory_name in _BASE_PROMPT_DIRECTORIES:
    for path in sorted((_PROMPTS_DIR / directory_name).glob('*')):
      if path.is_file():
        parts.append(path.read_text().strip())
  for name in _BASE_PROMPT_FILES:
    path = _PROMPTS_DIR / name
    if path.is_file():
      parts.append(path.read_text().strip())
  return '\n\n'.join(parts)


def session_append_prompt(hold: str, bro_name: str) -> str:
  """--append-system-prompt text for a ride-session (the non --raw flavor).

  base prompts plus the session bro's own persona prompts and spell
  instructions (`bro_name`, the `--bro` bro) — so a ride-session carries the
  bro's policies even though it runs the Claude Code harness. the assembled text renders
  once with this surface's facts: the claude harness over mcp wire names, with
  the session environment's credentials (this composes in the session's own
  process — in-container for container sessions — so the store is the scoped
  one). the hold fragment renders separately through
  `bro.prompts.hold_fragment` — the `#hold` fact is supplied only there, so the
  base and persona prompts stay hold-neutral.
  """
  # Keep the bro class graph out of this leaf module's import closure.
  import bro.mcp as mcp
  from bro import prompts
  from bro.base import credentials
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  parts = [_load_base_prompts(), bro.persona]
  spell_instructions = bro.spell_instructions()
  if len(spell_instructions) > 0:
    parts.append(spell_instructions)
  rendered = mcp.render_text(
    '\n\n'.join(parts),
    harness='claude',
    wire='mcp',
    creds=credentials.known_names(),
    extra=bro.vocabulary(),
  )
  fragment = prompts.hold_fragment(
    hold, harness='claude', wire='mcp', creds=credentials.known_names()
  )
  return f'{rendered}\n\n{fragment}'
