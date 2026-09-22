"""claude's own launch-context records for a `ride solo|along` session.

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
from pathlib import Path

RIDE_SESSION_CONTEXT_ENV = 'RIDE_SESSION_CONTEXT'

# the two names a repository's agent instructions go by, most canonical first —
# `AGENTS.md` is the cross-agent convention, `CLAUDE.md` the one Claude Code
# loads on its own
_INSTRUCTIONS_NAMES = ('AGENTS.md', 'CLAUDE.md')


def _mcp_record(bro: str) -> dict:
  fields = {'servers': [f'persona:{bro}']}
  return {'kind': 'mcp', 'subtype': 'servers', 'title': 'MCP servers', 'fields': fields}


def build_session_context(*, system_prompt: str, bro: str, proj_root: Path) -> list[dict]:
  """the launch-context records for a session.

  `bro` names the session's bro; `system_prompt` is the session's
  --append-system-prompt addition on top of claude's base plus whatever
  instructions it loads itself.
  """
  records: list[dict] = [
    {
      'kind': 'system_prompt',
      'subtype': 'ride_injected',
      'title': 'ride-injected system prompt (--append-system-prompt)',
      'content': system_prompt,
    },
    _mcp_record(bro),
  ]

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
