"""launch-context records for a `cw ss` session.

The model's system prompt and the repo policy it ran under are assembled inside
the claude process and never written to the JSONL transcript. cw knows the
session-shaping pieces at launch, so it captures them here as a list of typed
records, hands them to the session via the CW_SESSION_CONTEXT env var, and
the session recorder uploads them as the trail's launch-context attachment for `rewind` to render.

A record is `{kind, subtype, title}` plus either `content` (a text block) or
`fields` (an ordered key/value map). Add a record kind to enrich the inspection
view without touching the renderer.
"""

import json
from pathlib import Path
from typing import Optional

CW_SESSION_CONTEXT_ENV = 'CW_SESSION_CONTEXT'


def _mcp_record(bro: str, raw: bool) -> dict:
  if raw:
    fields = {'mode': 'bro', 'servers': [f'bro:{bro}']}
  else:
    fields = {'mode': 'persona', 'servers': [f'persona:{bro}']}
  return {'kind': 'mcp', 'subtype': 'servers', 'title': 'MCP servers', 'fields': fields}


def build_session_context(
  *,
  system_prompt: str,
  branch: str,
  base_sha: Optional[str],
  base_ref: Optional[str],
  bro: str,
  raw: bool,
  proj_root: Path,
) -> list[dict]:
  """the launch-context records for a session.

  `bro` names the session's bro; `raw` selects the system-prompt record's
  shape: a raw session passes the whole prompt via --system-prompt (replaces
  the base), a cw-session passes only its --append-system-prompt addition on
  top of claude's base + CLAUDE.md.
  """
  records: list[dict] = []

  if raw:
    sp_subtype, sp_title = 'bro', 'bro system prompt (--system-prompt, replaces base)'
  else:
    sp_subtype, sp_title = 'cw_injected', 'cw-injected system prompt (--append-system-prompt)'
  records.append(
    {'kind': 'system_prompt', 'subtype': sp_subtype, 'title': sp_title, 'content': system_prompt}
  )

  git_fields: dict = {'branch': branch}
  if base_sha is not None:
    git_fields['base_sha'] = base_sha
  if base_ref is not None:
    git_fields['base_ref'] = base_ref
  records.append(
    {'kind': 'git', 'subtype': 'state', 'title': 'git state at launch', 'fields': git_fields}
  )

  records.append(_mcp_record(bro, raw))

  claude_md = proj_root / 'CLAUDE.md'
  if claude_md.is_file():
    records.append(
      {
        'kind': 'claude_md',
        'subtype': 'root',
        'title': 'CLAUDE.md (root)',
        'content': claude_md.read_text().strip(),
      }
    )

  return records


def encode_session_context(records: list[dict]) -> str:
  return json.dumps(records)
