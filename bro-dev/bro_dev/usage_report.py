#!/usr/bin/env python
"""aggregate Claude Code token usage across a git commit range.

Walks `git log <range>`, parses the footer line emitted by
`setup/claude_commit_footer.py` (`> created with Claude Code <ver> (Model: N,NNN,
...; session: <id>)`), and sums per-model totals across every commit in the
range. Output is a small table — one line per model plus a grand total.

Example:
  usage-report master..HEAD
  usage-report HEAD~10..HEAD
  usage-report master~50..master
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass

from base import log
from base.args import Parser

__cli_name__ = 'usage-report'

_FOOTER_RE = re.compile(
  r'^>\s*created with Claude Code\s+\S+\s+\((.+?);\s*session:\s*(\S+?)\)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(r'^(.*?):\s*([\d,]+)$')


@dataclass
class CommitUsage:
  session_id: str
  per_model: dict[str, int]


def _parse_footer(commit_msg: str) -> CommitUsage | None:
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  parts_str, session_id = m.group(1), m.group(2)
  per_model: dict[str, int] = {}
  for chunk in parts_str.split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    model = pm.group(1).strip()
    n = int(pm.group(2).replace(',', ''))
    per_model[model] = per_model.get(model, 0) + n
  if len(per_model) == 0:
    return None
  return CommitUsage(session_id=session_id, per_model=per_model)


def _git_log(git_range: str) -> list[tuple[str, str]]:
  """returns [(commit_sha, commit_message)] for commits in range."""
  out = subprocess.check_output(
    ['git', 'log', '--pretty=format:%H%x1f%B%x1e', git_range], text=True
  )
  commits: list[tuple[str, str]] = []
  for record in out.split('\x1e'):
    record = record.strip()
    if len(record) == 0:
      continue
    sha, _, body = record.partition('\x1f')
    commits.append((sha, body))
  return commits


def _format_table(totals: dict[str, int], commit_count: int, footer_count: int) -> str:
  models = sorted(totals.keys())
  width = max((len(m) for m in models), default=0)
  width = max(width, len('total'))
  lines = [
    f'commits scanned: {commit_count}',
    f'commits with footer: {footer_count}',
    '',
  ]
  for m in models:
    lines.append(f'{m:<{width}}  {totals[m]:>15,}')
  grand = sum(totals.values())
  lines.append('-' * (width + 17))
  lines.append(f'{"total":<{width}}  {grand:>15,}')
  return '\n'.join(lines)


def usage_report(git_range: str) -> int:
  try:
    commits = _git_log(git_range)
  except subprocess.CalledProcessError as e:
    log.error('git log failed: %s', e)
    return 1

  totals: dict[str, int] = defaultdict(int)
  footer_count = 0
  for _sha, body in commits:
    parsed = _parse_footer(body)
    if parsed is None:
      continue
    footer_count += 1
    for model, n in parsed.per_model.items():
      totals[model] += n

  print(_format_table(totals, len(commits), footer_count))
  return 0


def main(argv=None):
  parser = Parser(description='aggregate Claude Code token usage across a git commit range')
  parser.add_argument(
    'git_range',
    help='git range, e.g. master..HEAD or HEAD~10..HEAD',
  )
  return usage_report(**parser.parse(argv))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
