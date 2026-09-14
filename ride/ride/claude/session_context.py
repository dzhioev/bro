"""launch-context records for a `ride solo|along` session.

The model's system prompt and the repo policy it ran under are assembled inside
the claude process and never written to the JSONL transcript. ride knows the
session-shaping pieces at launch, so it captures them here as a list of typed
records, hands them to the session via the RIDE_SESSION_CONTEXT env var, and
the session recorder uploads them as the trail's launch-context attachment for `rewind` to render.

A record is `{kind, subtype, title}` plus either `content` (a text block) or
`fields` (an ordered key/value map). Add a record kind to enrich the inspection
view without touching the renderer.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

RIDE_SESSION_CONTEXT_ENV = 'RIDE_SESSION_CONTEXT'

# the two names a repository's agent instructions go by, most canonical first —
# `AGENTS.md` is the cross-agent convention, `CLAUDE.md` the one Claude Code
# loads on its own
_INSTRUCTIONS_NAMES = ('AGENTS.md', 'CLAUDE.md')


@dataclass(frozen=True)
class GitState:
  """an attached session's git state at launch."""

  branch: str
  base_sha: str
  base_ref: Optional[str]


def _mcp_record(bro: str, raw: bool) -> dict:
  if raw:
    fields = {'mode': 'bro', 'servers': [f'bro:{bro}']}
  else:
    fields = {'mode': 'persona', 'servers': [f'persona:{bro}']}
  return {'kind': 'mcp', 'subtype': 'servers', 'title': 'MCP servers', 'fields': fields}


def build_session_context(
  *,
  system_prompt: str,
  git: Optional[GitState],
  bro: str,
  raw: bool,
  proj_root: Path,
) -> list[dict]:
  """the launch-context records for a session.

  `git` is an attached session's state, None for a detached one; `bro` names
  the session's bro; `raw` selects the system-prompt record's
  shape: a raw session passes the whole prompt via --system-prompt (replaces
  the base), a ride-session passes only its --append-system-prompt addition on
  top of claude's base plus whatever instructions it loads itself.
  """
  records: list[dict] = []

  if raw:
    sp_subtype, sp_title = 'bro', 'bro system prompt (--system-prompt, replaces base)'
  else:
    sp_subtype, sp_title = 'ride_injected', 'ride-injected system prompt (--append-system-prompt)'
  records.append(
    {'kind': 'system_prompt', 'subtype': sp_subtype, 'title': sp_title, 'content': system_prompt}
  )

  if git is not None:
    git_fields: dict = {'branch': git.branch, 'base_sha': git.base_sha}
    if git.base_ref is not None:
      git_fields['base_ref'] = git.base_ref
    records.append(
      {'kind': 'git', 'subtype': 'state', 'title': 'git state at launch', 'fields': git_fields}
    )

  records.append(_mcp_record(bro, raw))

  for name in _INSTRUCTIONS_NAMES:
    instructions = proj_root / name
    if instructions.is_file():
      records.append(
        {
          'kind': 'instructions',
          'subtype': 'root',
          'title': f'{name} (root)',
          'content': instructions.read_text().strip(),
        }
      )
      break

  return records


def encode_session_context(records: list[dict]) -> str:
  return json.dumps(records)
