#!/usr/bin/env python
"""aggregate LLM token usage across a git commit range.

Walks `git log <range>`, parses the footer emitted by
`bro/workflow/commit_footer.py` (format owned by the `usage` module), and sums
the per-model, per-class *deltas* across every commit in the range — the total
spent producing it, each session counted once for what it had spent by the time
its last commit was written.

The four token classes (input / cache-write / cache-read / output) are reported
separately: they differ in price by up to ~50x, so a single summed number would be
dominated by cache-read and meaningless as spend. Commits without a parseable
footer are not counted.

Example:
  usage-report master..HEAD
  usage-report HEAD~10..HEAD
  usage-report master~50..master
"""

from __future__ import annotations

import subprocess
from typing import Optional

import bro.llm.usage as usage
from bro.base import log
from bro.base.args import Parser
from bro.llm.usage import Counts

__cli_name__ = 'usage-report'

_COLUMN_HEADER = {
  'input': 'input',
  'cache_write': 'cache-write',
  'cache_read': 'cache-read',
  'output': 'output',
}


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


def _format_table(totals: dict[str, Counts], commit_count: int, summed_count: int) -> str:
  models = sorted(totals.keys())
  grand = usage.zero()
  for c in totals.values():
    grand = usage.add(grand, c)

  name_width = max([len(m) for m in models] + [len('model'), len('total')])
  column_widths = {
    c: max(
      [len(usage.format_int(totals[m].get(c, 0))) for m in models]
      + [len(usage.format_int(grand[c])), len(_COLUMN_HEADER[c])]
    )
    for c in usage.CLASSES
  }

  def _row(label: str, counts: Counts) -> str:
    cells = '  '.join(
      f'{usage.format_int(counts.get(c, 0)):>{column_widths[c]}}' for c in usage.CLASSES
    )
    return f'{label:<{name_width}}  {cells}'

  header = f'{"model":<{name_width}}  ' + '  '.join(
    f'{_COLUMN_HEADER[c]:>{column_widths[c]}}' for c in usage.CLASSES
  )
  lines = [
    f'commits scanned: {commit_count}',
    f'footers summed: {summed_count}',
    '',
    header,
  ]
  for m in models:
    lines.append(_row(m, totals[m]))
  lines.append('-' * len(header))
  lines.append(_row('total', grand))
  return '\n'.join(lines)


def usage_report(git_range: str) -> int:
  try:
    commits = _git_log(git_range)
  except subprocess.CalledProcessError as e:
    log.error('git log failed: %s', e)
    return 1

  totals: dict[str, Counts] = {}
  summed_count = 0
  for _, body in commits:
    parsed = usage.parse_footer(body)
    if parsed is not None:
      summed_count += 1
      for model, c in parsed.delta.items():
        totals[model] = usage.add(totals.get(model, usage.zero()), c)

  print(_format_table(totals, len(commits), summed_count))
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='aggregate LLM token usage across a git commit range')
  parser.add_argument(
    'git_range',
    help='git range, e.g. master..HEAD or HEAD~10..HEAD',
  )
  return usage_report(**parser.parse(argv))
