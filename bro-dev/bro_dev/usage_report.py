#!/usr/bin/env python
"""aggregate Claude Code token usage across a git commit range.

Walks `git log <range>`, parses the footer emitted by
`setup/claude_commit_footer.py`, and sums the per-model, per-class *deltas* across
every commit in the range — which, under the squash-merge workflow, equals the true
total spent producing that range (each session counted once).

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

import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from base import log
from base.args import Parser

__cli_name__ = 'usage-report'

_THOUSANDS = "'"
_CLASSES = ('input', 'cache_write', 'cache_read', 'output')
_COL_HEADER = {
  'input': 'input',
  'cache_write': 'cache-write',
  'cache_read': 'cache-read',
  'output': 'output',
}

Counts = dict[str, int]

# footer parser (owned by setup/claude_commit_footer.py; duplicated here because
# sharing would require making setup/ a Python package, which is out of scope).
_FOOTER_RE = re.compile(
  r'^>\s*created with Claude Code\s+(?P<versions>.+?)\s*\|\s*(?P<tokens>.+?)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(
  r'^(?P<model>.*?):\s*'
  r'↑\s*(?P<input>[\d\']+)\s*/\s*(?P<cache_write>[\d\']+)\s*'
  r'\(\s*(?P<cache_read>[\d\']+)\s*\)\s*'
  r'↓\s*(?P<output>[\d\']+)$'
)


def _zero() -> Counts:
  return dict.fromkeys(_CLASSES, 0)


def _add(a: Counts, b: Counts) -> Counts:
  return {c: a.get(c, 0) + b.get(c, 0) for c in _CLASSES}


def _fmt_int(n: int) -> str:
  return f'{n:,}'.replace(',', _THOUSANDS)


@dataclass
class CommitUsage:
  per_model: dict[str, Counts]


def _parse_footer(commit_msg: str) -> Optional[CommitUsage]:
  """parse the four-class footer; returns None if it is absent or unparseable."""
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  per_model: dict[str, Counts] = {}
  for chunk in m.group('tokens').split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    model = pm.group('model').strip()
    counts = {c: int(pm.group(c).replace(_THOUSANDS, '')) for c in _CLASSES}
    per_model[model] = _add(per_model.get(model, _zero()), counts)
  if len(per_model) == 0:
    return None
  return CommitUsage(per_model=per_model)


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
  grand = _zero()
  for c in totals.values():
    grand = _add(grand, c)

  name_w = max([len(m) for m in models] + [len('model'), len('total')])
  col_w = {
    c: max(
      [len(_fmt_int(totals[m].get(c, 0))) for m in models]
      + [len(_fmt_int(grand[c])), len(_COL_HEADER[c])]
    )
    for c in _CLASSES
  }

  def _row(label: str, counts: Counts) -> str:
    cells = '  '.join(f'{_fmt_int(counts.get(c, 0)):>{col_w[c]}}' for c in _CLASSES)
    return f'{label:<{name_w}}  {cells}'

  header = f'{"model":<{name_w}}  ' + '  '.join(f'{_COL_HEADER[c]:>{col_w[c]}}' for c in _CLASSES)
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
    parsed = _parse_footer(body)
    if parsed is not None:
      summed_count += 1
      for model, c in parsed.per_model.items():
        totals[model] = _add(totals.get(model, _zero()), c)

  print(_format_table(totals, len(commits), summed_count))
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='aggregate Claude Code token usage across a git commit range')
  parser.add_argument(
    'git_range',
    help='git range, e.g. master..HEAD or HEAD~10..HEAD',
  )
  return usage_report(**parser.parse(argv))
