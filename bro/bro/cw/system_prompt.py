from pathlib import Path
from typing import Optional

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'
# auto-injected into every `cw ss` session via --append-system-prompt. Files in
# `shared/` also flow into every bro (via bro/bro.py:_load_shared_prompts), so
# put cross-surface conventions there. Prompt files may carry template
# directives (`#harness`/`#wire`/`#creds`) — the whole append text renders once
# in `_session_append_prompt` with this surface's facts. Top-level files here
# are Claude-Code-only:
# - `environment.md` is the single source of truth for the session-banner
#   playbook (forked on `#harness`: `bro::banner` tool vs `cw banner --llm`) —
#   same file is reachable from bros via `FileSource` (bro/bros/ppp_dev).
# - `tool_names.md` is the tool-name resolution rule, templated on `#wire`;
#   `--bro` sessions run `--bare` and skip this injection but get the same file
#   through `BaseBro.claude_system_prompt`, and bro-native LLM runs compose its
#   bare rendering — do not give a bro a `FileSource` for this file.
_BASE_PROMPT_DIRS = ['shared']
_BASE_PROMPT_FILES = ['environment.md', 'tool_names.md']


def _mode_prompt(auto: bool) -> str:
  """the session-mode tail injected at launch: the autonomous or manual fragment
  (the pick-one pair documented in prompts/CLAUDE.md), plus the land-mode hint
  for autonomous sessions."""
  if auto:
    fragment = (_PROMPTS_DIR / 'autonomous_session.md').read_text().strip()
    return f'{fragment}\n\nLand mode: PR'
  return (_PROMPTS_DIR / 'manual_session.md').read_text().strip()


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


def _session_append_prompt(auto: bool, bro_name: Optional[str]) -> str:
  """--append-system-prompt text for a non --bro `cw ss` session.

  base prompts plus, when launched for a bro (dive-in sets CW_BRO), that bro's
  persona — so dive-in carries ppp-dev's policies even though it runs the native
  Claude Code harness rather than --bro. the assembled text renders once with
  this surface's facts: the native claude harness over mcp wire names, with the
  session environment's credentials (this composes in the session's own process
  — in-container for container sessions — so the store is the scoped one).
  """
  # llm.mcp and bro.registry are imported lazily — the hub aggregates every cw
  # submodule, so a module-level import here would tax every `import cw`.
  import llm.mcp
  from base import credentials

  parts = [_load_base_prompts()]
  if bro_name is not None:
    from bro.registry import create_bro

    parts.append(create_bro(bro_name).persona)
  parts.append(_mode_prompt(auto))
  return llm.mcp.render_text(
    '\n\n'.join(parts), harness='claude', wire='mcp', creds=credentials.known_names()
  )
