#!/usr/bin/env python
"""Fold the trails recorded in a window into a Markdown usage report.

Figures come from the aggregate each trail header already carries, so a run is
one paged list query rather than a walk of every step. `--verify` spot-checks
that aggregate against the trail's own per-call stream for the heaviest trails
in the window, which is the only independent count of the same quantity the
store offers.
"""

import collections
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol

import bro.llm.usage as usage
from bro.base import credentials
from bro.base.args import Parser
from bro.trails.client import TrailsClient
from bro.workspace.paths import project_root
from bro.workspace.project import project_config


class MessageReader(Protocol):
  """the one thing the reconciliation pass needs of a trails client."""

  def iter_messages(self, trail_id: str, *, types: Optional[set[str]] = None) -> Iterator[dict]: ...


DEFAULT_DAYS = 30
DEFAULT_VERIFY = 10
# a spot-check walks a trail's whole step stream, so the client outlives the
# short timeout a header listing needs
CLIENT_TIMEOUT_SECONDS = 120.0


@dataclass
class Window:
  since: datetime
  until: datetime

  @property
  def days(self) -> int:
    return (self.until - self.since).days

  def stamp(self, moment: datetime) -> str:
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


@dataclass
class Fold:
  """per-class token counts cut every way the report reports them."""

  trails: int = 0
  live: int = 0
  by_vendor: dict[str, usage.Counts] = field(default_factory=dict)
  by_model: dict[str, usage.Counts] = field(default_factory=dict)
  by_bro: dict[str, usage.Counts] = field(default_factory=dict)
  by_harness: dict[str, usage.Counts] = field(default_factory=dict)
  sessions_by_bro: collections.Counter = field(default_factory=collections.Counter)

  def total(self) -> usage.Counts:
    totals = usage.zero()
    for counts in self.by_vendor.values():
      totals = usage.add(totals, counts)
    return totals


def _accumulate(target: dict[str, usage.Counts], key: str, counts: usage.Counts) -> None:
  target[key] = usage.add(target.get(key, usage.zero()), counts)


def fold_headers(headers: list[dict]) -> Fold:
  fold = Fold()
  for header in headers:
    fold.trails += 1
    if header.get('end') is None:
      fold.live += 1
    bro = header.get('bro') or '(none)'
    fold.sessions_by_bro[bro] += 1
    for model, raw in (header.get('usage') or {}).items():
      counts = usage.from_vendor_counts(raw)
      _accumulate(fold.by_vendor, usage.vendor_of(model), counts)
      _accumulate(fold.by_model, usage.model_family(model), counts)
      _accumulate(fold.by_bro, bro, counts)
      _accumulate(fold.by_harness, header['harness'], counts)
  return fold


def header_total(header: dict) -> usage.Counts:
  totals = usage.zero()
  for raw in (header.get('usage') or {}).values():
    totals = usage.add(totals, usage.from_vendor_counts(raw))
  return totals


def call_total(client: MessageReader, trail_id: str) -> usage.Counts:
  """the same trail's spend counted from its own per-call stream."""
  totals = usage.zero()
  for message in client.iter_messages(trail_id, types={'llm_call'}):
    call_usage = message.get('usage')
    if call_usage is not None:
      totals = usage.add(totals, usage.from_vendor_counts(call_usage))
  return totals


@dataclass
class Discrepancy:
  trail_id: str
  header: usage.Counts
  calls: usage.Counts


def verify(client: MessageReader, headers: list[dict], count: int) -> list[Discrepancy]:
  """re-count the heaviest trails from their call streams; return the ones that disagree."""
  heaviest = sorted(headers, key=lambda h: sum(header_total(h).values()), reverse=True)[:count]
  discrepancies = []
  for header in heaviest:
    from_header = header_total(header)
    from_calls = call_total(client, header['id'])
    if from_header != from_calls:
      discrepancies.append(Discrepancy(header['id'], from_header, from_calls))
  return discrepancies


def _row(label: str, counts: usage.Counts) -> str:
  cells = ' | '.join(usage.format_int(counts.get(c, 0)) for c in usage.CLASSES)
  return f'| {label} | {cells} |'


def _table(counts_by_key: dict[str, usage.Counts], key_header: str) -> list[str]:
  lines = [
    f'| {key_header} | ' + ' | '.join(usage.CLASSES) + ' |',
    '|' + '---|' * (len(usage.CLASSES) + 1),
  ]
  ranked = sorted(counts_by_key.items(), key=lambda item: -item[1]['output'])
  lines.extend(_row(key, counts) for key, counts in ranked)
  return lines


def uploaded(counts: usage.Counts) -> int:
  """every token class billed on the way in, cached or not."""
  return counts['input'] + counts['cache_write'] + counts['cache_read']


def _balance_row(label: str, counts: usage.Counts) -> str:
  written, read = counts['cache_write'], counts['cache_read']
  # the interesting rows are the low ones; they must not round up to a whole
  # read-back they never got
  ratio = None if written == 0 else read / written
  reuse = '—' if ratio is None else f'{ratio:.1f}x' if ratio < 10 else f'{ratio:.0f}x'
  total = uploaded(counts)
  share = '—' if total == 0 else f'{100 * written / total:.1f}%'
  cached = '—' if total == 0 else f'{100 * (written + read) / total:.1f}%'
  return f'| {label} | {usage.format_int(written)} | {usage.format_int(read)} | {reuse} | {share} | {cached} |'


def _balance_table(counts_by_key: dict[str, usage.Counts], key_header: str) -> list[str]:
  lines = [
    f'| {key_header} | cache_write | cache_read | read per write | write share of upload |'
    ' cached share of upload |',
    '|---|---|---|---|---|---|',
  ]
  ranked = sorted(counts_by_key.items(), key=lambda item: -item[1]['cache_read'])
  lines.extend(_balance_row(key, counts) for key, counts in ranked)
  return lines


def render(
  window: Window,
  fold: Fold,
  discrepancies: list[Discrepancy],
  verified: int,
  generated: datetime,
) -> str:
  totals = fold.total()
  lines = [
    f'# Usage report — {window.since.date()} → {window.until.date()}',
    '',
    f'Window: `{window.stamp(window.since)}` … `{window.stamp(window.until)}` ({window.days} days). '
    f'{fold.trails} trails, {fold.live} of them still live when the report was generated.',
    '',
    f'Generated `{window.stamp(generated)}`. A window whose trails were still running when this '
    'ran will fold to larger figures if it is regenerated later.',
    '',
    'Token counts are the four billed classes, kept apart: they differ in price by up to ~50x, '
    'so their sum is not an amount of anything. Shares are taken within a class.',
    '',
    '## Totals',
    '',
    *_table({'all': totals}, 'scope'),
    '',
    '## By vendor',
    '',
    *_table(fold.by_vendor, 'vendor'),
    '',
    '## By model',
    '',
    *_table(fold.by_model, 'model'),
    '',
    '## By bro',
    '',
    *_table(fold.by_bro, 'bro'),
    '',
    '## By harness',
    '',
    *_table(fold.by_harness, 'harness'),
    '',
    '## Cache balance',
    '',
    'How much of each upload was cache traffic, and how many times a written prefix was read '
    'back. A prefix written and never re-read costs more than not caching it at all, so the '
    'read-per-write figure is what says whether the caching paid.',
    '',
    *_balance_table({'all': totals}, 'scope'),
    '',
    *_balance_table(fold.by_vendor, 'vendor'),
    '',
    *_balance_table(fold.by_bro, 'bro'),
    '',
    '## Sessions',
    '',
    '| bro | trails |',
    '|---|---|',
    *(f'| {bro} | {count} |' for bro, count in fold.sessions_by_bro.most_common()),
    '',
    '## Reconciliation',
    '',
  ]
  if verified == 0:
    lines.append('Not checked — this run was generated with `--verify 0`.')
  elif len(discrepancies) == 0:
    lines.append(
      f'The {verified} heaviest trails in the window were re-counted from their own per-call '
      'streams. Every one reproduced its header aggregate exactly.'
    )
  else:
    lines.append(
      f'{len(discrepancies)} of the {verified} heaviest trails did not reproduce their header '
      'aggregate. The figures above are drawn from the headers and are unreconciled:'
    )
    lines.append('')
    for discrepancy in discrepancies:
      drift = usage.subtract(discrepancy.calls, discrepancy.header)
      lines.append(f'- `{discrepancy.trail_id}`: calls − header = {drift}')
  lines.extend(['', 'Generated by `bros/analyst/scripts/trails_usage.py`.', ''])
  return '\n'.join(lines)


def resolve_window(since: Optional[str], until: Optional[str], days: int) -> Window:
  end = datetime.now(UTC) if until is None else datetime.fromisoformat(until).astimezone(UTC)
  start = (
    end - timedelta(days=days) if since is None else datetime.fromisoformat(since).astimezone(UTC)
  )
  if start >= end:
    raise ValueError(f'empty window: {start.isoformat()} .. {end.isoformat()}')
  return Window(start, end)


def report_name(slug: str, generated: datetime) -> str:
  """`<generation date>–<slug>.md`.

  The date is when the figures were pulled, not the window they cover — a
  backfilled window says so in its slug, and a regenerated one sorts after the
  report it supersedes. The en dash keeps that boundary visible against the
  hyphens inside the slug.
  """
  cleaned = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')
  if cleaned == '':
    raise ValueError(f'slug {slug!r} carries no name')
  return f'{generated.date()}–{cleaned}.md'


SECTION = 'analyst'


def reports_directory() -> Path:
  """the operated repo's declared reports directory.

  Every repo names its own, because an installed framework's package directory
  is site-packages and no place to commit anything. Absent, the run stops here
  rather than guessing at a location the repo would have to live with.
  """
  configured = project_config().sections.get(SECTION, {}).get('reports')
  if not isinstance(configured, str) or configured == '':
    raise ValueError(
      f'{project_root() / "pyproject.toml"} declares no [tool.bro.{SECTION}] reports directory; '
      'add one naming where this repo keeps its analyses'
    )
  return project_root() / configured


def resolve_destination(
  output: Optional[str], slug: str, generated: datetime, *, force: bool
) -> Path:
  """where the report is written, refusing to discard a report already there.

  A written report carries a reading this script does not produce and cannot
  reproduce, so overwriting one has to be asked for.
  """
  if output is not None:
    destination = Path(output)
  else:
    directory = reports_directory()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / report_name(slug, generated)
  if destination.exists() and not force:
    raise FileExistsError(f'{destination} already exists; pass --force to replace it')
  return destination


def make_client() -> TrailsClient:
  config = credentials.get_json('trails')
  return TrailsClient(config['base_url'], config['token'], timeout=CLIENT_TIMEOUT_SECONDS)


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='render a Markdown usage report over the recorded trails')
  parser.add_argument('--since', help='ISO start of the window (default: --days before --until)')
  parser.add_argument('--until', help='ISO end of the window (default: now)')
  parser.add_argument(
    '--days', type=int, default=DEFAULT_DAYS, help='window length when --since is absent'
  )
  parser.add_argument(
    '--verify',
    type=int,
    default=DEFAULT_VERIFY,
    help='re-count this many of the heaviest trails from their call streams (0 skips the check)',
  )
  parser.add_argument(
    '--slug',
    required=True,
    help='what this report is about, e.g. "june-2026 cache balance"; names the file with the date',
  )
  parser.add_argument(
    '--output', help='write here instead of <[tool.bro] reports>/<date>–<slug>.md'
  )
  parser.add_argument(
    '--force', action='store_true', help='overwrite an existing report, discarding its reading'
  )
  args = parser.parse(argv)

  generated = datetime.now(UTC)
  window = resolve_window(args['since'], args['until'], args['days'])
  destination = resolve_destination(args['output'], args['slug'], generated, force=args['force'])
  with make_client() as client:
    headers = list(
      client.iter_trails(since=window.stamp(window.since), until=window.stamp(window.until))
    )
    fold = fold_headers(headers)
    discrepancies = verify(client, headers, args['verify']) if args['verify'] > 0 else []

  destination.write_text(render(window, fold, discrepancies, args['verify'], generated))
  print(f'{destination}: {fold.trails} trails, {len(discrepancies)} unreconciled')
  return None


if __name__ == '__main__':
  raise SystemExit(main(sys.argv))
