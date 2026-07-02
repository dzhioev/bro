from pathlib import Path
from typing import Optional

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'
# auto-injected into every `cw ss` session via --append-system-prompt. Files in
# `shared/` also flow into every bro (via bro/bro.py:_load_shared_prompts), so
# put cross-surface conventions there. Top-level files here are Claude-Code-only:
# - `environment.md` is the single source of truth for the cw-banner playbook —
#   same file is reachable from bros via `FileSource` (bro/bros/ppp_dev).
# - `tool_names.md` is the Claude-Code tool-name resolution rule (`ns::tool` →
#   `mcp__ns__tool`). `--bro` sessions run `--bare` and skip this injection but
#   get the same file through `BaseBro.claude_system_prompt`; bro-native LLM
#   runs use the `_TOOL_NAMES_BLOCK` in bro/bro.py instead (`ns__tool`, no
#   `mcp__`) — do not give a bro a `FileSource` for this file.
_BASE_PROMPT_DIRS = ['shared']
_BASE_PROMPT_FILES = ['environment.md', 'tool_names.md']


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
  Claude Code harness rather than --bro.
  """
  parts = [_load_base_prompts()]
  if bro_name is not None:
    from bro.registry import create_bro

    parts.append(create_bro(bro_name).persona)
  if auto:
    parts.append('Land mode: PR')
  return '\n\n'.join(parts)
