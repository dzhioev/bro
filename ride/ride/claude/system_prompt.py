from pathlib import Path

import bro.prompts as prompts

_PROMPTS_DIR = Path(prompts.__file__).parent
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
  """--append-system-prompt text for a ride-session.

  base prompts plus the session bro's own persona prompts and spell
  instructions (`bro_name`, the `--bro` bro) — so a ride-session carries the
  bro's policies even though it runs the Claude Code harness. the assembled text renders
  once with this surface's facts: the claude harness, the session's `hold`, and
  the session environment's credentials (this composes in the session's own
  process — in-container for container sessions — so the store is the scoped
  one, and the summoned mark is this session's own). the session fragments
  follow, rendered through `bro.prompts.session_fragment`.
  """
  # Keep the bro class graph out of this leaf module's import closure.
  import bro.mcp as mcp
  from bro import prompts, summon
  from bro.base import credentials
  from bro.registry import create_bro
  from ride.claude.harness import CLAUDE

  bro = create_bro(bro_name)
  parts = [_load_base_prompts(), bro.persona]
  spell_instructions = bro.spell_instructions()
  if len(spell_instructions) > 0:
    parts.append(spell_instructions)
  rendered = mcp.render_text(
    '\n\n'.join(parts),
    harness=CLAUDE,
    creds=credentials.known_names(),
    may_summon=summon.effective_may_summon(),
    hold=hold,
    extra=bro.vocabulary(),
  )
  fragment = prompts.session_fragment(
    hold,
    harness=CLAUDE,
    creds=credentials.known_names(),
    talk=summon.talk(),
  )
  return f'{rendered}\n\n{fragment}'
