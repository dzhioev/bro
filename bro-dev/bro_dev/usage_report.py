#!/usr/bin/env python
"""aggregate Claude Code token usage across a git commit range.

Walks `git log <range>`, parses the two-line footer emitted by
`setup/claude_commit_footer.py`, and sums the per-model *deltas* across every
commit in the range — which, under the squash-merge workflow, equals the true
total spent producing that range (each session counted once).

Legacy single-line footers (`> created with Claude Code <ver> (<model>: N,NNN;
session: <id>)`) carry a session *cumulative*, not a delta, and are not summable;
they are counted as legacy, excluded from the total, and reported with a warning
so the number is honestly incomplete rather than silently wrong.

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

# two-line delta footer (owned by setup/claude_commit_footer.py)
_FOOTER_RE = re.compile(
  r'^>\s*created with Claude Code\s+(?P<versions>.+?)\s*\|\s*(?P<tokens>.+?)\s*\n'
  r'>\s*session\(s\):\s*(?P<sessions>.+?)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(r"^(?P<model>.*?):\s*(?P<n>[\d']+)$")

# legacy single-line cumulative footer (pre-redesign; not summable)
_LEGACY_RE = re.compile(
  r'^>\s*created with Claude Code\s+\S+\s+\(.+?;\s*session:\s*\S+?\)\s*$',
  re.MULTILINE,
)


@dataclass
class CommitUsage:
  session_ids: list[str]
  per_model: dict[str, int]


def _parse_footer(commit_msg: str) -> CommitUsage | None:
  """parse the two-line delta footer; returns None if it is absent or legacy."""
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  per_model: dict[str, int] = {}
  for chunk in m.group('tokens').split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    model = pm.group('model').strip()
    per_model[model] = per_model.get(model, 0) + int(pm.group('n').replace("'", ''))
  if len(per_model) == 0:
    return None
  sessions = [s.strip() for s in m.group('sessions').split(', ')]
  return CommitUsage(session_ids=sessions, per_model=per_model)


def _is_legacy_footer(commit_msg: str) -> bool:
  return _LEGACY_RE.search(commit_msg) is not None


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


def _format_table(
  totals: dict[str, int], commit_count: int, new_count: int, legacy_count: int
) -> str:
  models = sorted(totals.keys())
  width = max((len(m) for m in models), default=0)
  width = max(width, len('total'))
  lines = [
    f'commits scanned: {commit_count}',
    f'delta footers summed: {new_count}',
    f'legacy footers skipped: {legacy_count}',
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
  new_count = 0
  legacy_shas: list[str] = []
  for sha, body in commits:
    parsed = _parse_footer(body)
    if parsed is not None:
      new_count += 1
      for model, n in parsed.per_model.items():
        totals[model] += n
    elif _is_legacy_footer(body):
      legacy_shas.append(sha[:9])

  if len(legacy_shas) > 0:
    log.warning(
      '%d legacy-footer commit(s) excluded from the total (cumulative, not deltas): %s',
      len(legacy_shas),
      ', '.join(legacy_shas),
    )

  print(_format_table(dict(totals), len(commits), new_count, len(legacy_shas)))
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
