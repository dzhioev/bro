#!/usr/bin/env python3
"""prints a git commit footer crediting the current Claude Code session."""

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
  cwd = Path(os.environ.get('PWD') or os.getcwd()).resolve()
  for candidate in [cwd, *cwd.parents]:
    project_dir = projects_root / _encode_cwd(str(candidate))
    if project_dir.is_dir():
      jsonls = sorted(project_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
      if jsonls:
        return jsonls[-1]
  raise SystemExit(f'no Claude Code session transcript found for {cwd}')


def _last_usage(path: Path) -> tuple[str, int]:
  with path.open() as f:
    lines = f.readlines()
  for line in reversed(lines):
    entry = json.loads(line)
    msg = entry.get('message')
    if not isinstance(msg, dict) or 'usage' not in msg:
      continue
    u = msg['usage']
    total = (
      u.get('input_tokens', 0)
      + u.get('cache_creation_input_tokens', 0)
      + u.get('cache_read_input_tokens', 0)
      + u.get('output_tokens', 0)
    )
    return msg.get('model', 'unknown'), total
  raise SystemExit(f'no assistant usage recorded yet in {path.name}')


def _model_label(slug: str) -> str:
  m = re.match(r'^claude-(opus|sonnet|haiku)-(\d+)-(\d+)', slug)
  if not m:
    return slug
  family, maj, minor = m.groups()
  return f'{family.title()} {maj}.{minor}'


def _format_tokens(n: int) -> str:
  if n >= 1_000_000:
    return f'{n / 1_000_000:.1f}M'
  if n >= 1_000:
    return f'{round(n / 1000)}k'
  return str(n)


def _version() -> str:
  execpath = os.environ.get('CLAUDE_CODE_EXECPATH')
  return Path(execpath).name if execpath else 'unknown'


def main() -> int:
  path = _find_session_jsonl()
  model_slug, tokens = _last_usage(path)
  print(
    f'> created with Claude Code {_version()} '
    f'({_model_label(model_slug)}, context: {_format_tokens(tokens)})'
  )
  return 0


if __name__ == '__main__':
  sys.exit(main())
