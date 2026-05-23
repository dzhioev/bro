#!/usr/bin/env python3
"""prints a git commit footer crediting the current Claude Code session.

Walks the active session jsonl, sums token usage per model, and emits a one-line
footer carrying precise per-model totals plus the session id. Totals are printed
with thousands-separator commas so a downstream aggregator can recover the exact
integers (`usage-report` does this — sums per-model totals across a git range).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _encode_cwd(cwd: str) -> str:
  return cwd.replace('/', '-').replace('.', '-')


def _find_session_jsonl() -> Path:
  projects_root = Path.home() / '.claude' / 'projects'
  pwd = os.environ.get('PWD')
  cwd = Path(pwd if pwd is not None else os.getcwd()).resolve()
  for candidate in [cwd, *cwd.parents]:
    project_dir = projects_root / _encode_cwd(str(candidate))
    if project_dir.is_dir():
      jsonls = sorted(project_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
      if len(jsonls) > 0:
        return jsonls[-1]
  raise SystemExit(f'no Claude Code session transcript found for {cwd}')


def _cumulative_usage(path: Path) -> dict[str, int]:
  """returns {model_slug: total_tokens} summed across every assistant message."""
  totals: dict[str, int] = {}
  with path.open() as f:
    for line in f:
      try:
        entry = json.loads(line)
      except json.JSONDecodeError:
        continue
      msg = entry.get('message')
      if not isinstance(msg, dict):
        continue
      u = msg.get('usage')
      if not isinstance(u, dict):
        continue
      model = msg.get('model')
      if not isinstance(model, str):
        model = 'unknown'
      total = (
        int(u.get('input_tokens', 0) or 0)
        + int(u.get('cache_creation_input_tokens', 0) or 0)
        + int(u.get('cache_read_input_tokens', 0) or 0)
        + int(u.get('output_tokens', 0) or 0)
      )
      totals[model] = totals.get(model, 0) + total
  if len(totals) == 0:
    raise SystemExit(f'no assistant usage recorded yet in {path.name}')
  return totals


def _model_label(slug: str) -> str:
  m = re.match(r'^claude-(opus|sonnet|haiku)-(\d+)-(\d+)', slug)
  if m is None:
    return slug
  family, maj, minor = m.groups()
  return f'{family.title()} {maj}.{minor}'


def _version() -> str:
  execpath = os.environ.get('CLAUDE_CODE_EXECPATH')
  return Path(execpath).name if execpath is not None else 'unknown'


def main() -> int:
  path = _find_session_jsonl()
  totals = _cumulative_usage(path)
  parts = [f'{_model_label(m)}: {t:,}' for m, t in totals.items()]
  print(f'> created with Claude Code {_version()} ({", ".join(parts)}; session: {path.stem})')
  return 0


if __name__ == '__main__':
  sys.exit(main())
