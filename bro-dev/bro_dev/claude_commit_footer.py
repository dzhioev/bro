#!/usr/bin/env python3
"""prints a git commit footer crediting the Claude Code token spend behind a commit.

The footer is the offline-readable cache of a session's token spend, attributed to
the commits it produced, so that summing a per-commit *delta* across `git log`
yields the true total for a range — and stays correct across squash merges.

Per-model usage is reported as the four token classes the Anthropic API bills
separately — they differ in price by up to ~50x, so a single summed number would
be dominated by `cache_read` and mean nothing as spend. Each class is kept
distinct:

- input        — fresh, uncached prompt tokens (full price)
- cache_write  — tokens written to the prompt cache (1.25x)
- cache_read   — tokens served from the prompt cache (0.1x); in a long agentic
                 session this dominates by volume but not by cost (re-reads of the
                 growing prefix)
- output       — generated tokens (5x)

Three modes:
- default: emit the footer for the commit about to be made. The per-class delta is
  this session's cumulative transcript usage now minus the baseline already
  attributed to its earlier commits (read from the state file). Also stages the
  new cumulative for the post-commit hook to promote.
- --record: promote the staged cumulative to the committed baseline. Run by the
  post-commit git hook once a commit actually lands, so the mark only advances
  after a *successful* commit (retries and footerless commits stay correct).
- --squash <range>: emit an aggregated footer for a squash merge — the sum of
  every branch commit's per-class deltas / the union of versions plus the land
  session's uncommitted remainder. Run by /land and injected into the PR body, so
  the server-side squash commit carries the sum of the children it discards.

Footer shape (one `>`-quoted line; `'` thousands separator so it never collides
with the `, ` joining model entries):

  > created with Claude Code <versions> | <model>: ↑ <input> / <cache_write> (<cache_read>) ↓ <output>[, …]

`↑` marks the upload group (input `/` cache_write, with cache_read parenthesized);
`↓` marks output.

State lives in a gitignored `<repo>/.token_accounting_state.json`; git history
carries only the durable per-class deltas / versions. Runs through base.args, so
it needs the project venv active (the editable install puts `base` on the path even
when invoked by file path); the post-commit hook surfaces a failure rather than
swallowing it, so committing without the venv is caught.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base.args import Parser

STATE_FILENAME = '.token_accounting_state.json'
_THOUSANDS = "'"
_UP = '↑'
_DOWN = '↓'

# claude code labels locally-generated assistant turns (interrupts, local errors,
# injected notices) with this sentinel model — no real API round-trip, so their
# usage is not billed spend and must not be credited to any commit.
_SYNTHETIC_MODEL = '<synthetic>'

# the four billed token classes, in footer display order, mapped to their
# transcript `usage` field names.
_CLASSES = ('input', 'cache_write', 'cache_read', 'output')
_FIELD_OF = {
  'input': 'input_tokens',
  'cache_write': 'cache_creation_input_tokens',
  'cache_read': 'cache_read_input_tokens',
  'output': 'output_tokens',
}

# a per-model usage record is a plain dict keyed by _CLASSES (JSON-friendly for the
# state file; arithmetic via the helpers below).
Counts = dict[str, int]


def _zero() -> Counts:
  return dict.fromkeys(_CLASSES, 0)


def _add(a: Counts, b: Counts) -> Counts:
  return {c: a.get(c, 0) + b.get(c, 0) for c in _CLASSES}


def _sub(a: Counts, b: Counts) -> Counts:
  return {c: a.get(c, 0) - b.get(c, 0) for c in _CLASSES}


def _effective_baseline(committed: Counts, cum: Counts) -> Counts:
  """the cumulative to subtract for a delta.

  Normally the committed mark. But within one session every class only grows, so a
  current cumulative that is smaller in any class means the transcript reset — a
  different session reused this worktree's state file. Treat the baseline as zero
  so the commit is credited its full new cumulative, not a bogus negative delta.
  """
  if any(cum.get(c, 0) < committed.get(c, 0) for c in _CLASSES):
    return _zero()
  return committed


# --- transcript reading -----------------------------------------------------


def _encode_cwd(cwd: str) -> str:
  return cwd.replace('/', '-').replace('.', '-')


def _find_session_jsonl() -> Optional[Path]:
  projects_root = Path.home() / '.claude' / 'projects'
  pwd = os.environ.get('PWD')
  cwd = Path(pwd if pwd is not None else os.getcwd()).resolve()
  for candidate in [cwd, *cwd.parents]:
    project_dir = projects_root / _encode_cwd(str(candidate))
    if project_dir.is_dir():
      jsonls = sorted(project_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
      if len(jsonls) > 0:
        return jsonls[-1]
  return None


def _cumulative_usage(path: Path) -> dict[str, Counts]:
  """returns {model_slug: per-class totals} summed across every billed assistant message.

  synthetic turns (model `<synthetic>`) are skipped — they carry no real API spend.
  """
  totals: dict[str, Counts] = {}
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
      if model == _SYNTHETIC_MODEL:
        continue
      counts = {c: int(v) for c in _CLASSES if (v := u.get(_FIELD_OF[c])) is not None}
      totals[model] = _add(totals.get(model, _zero()), counts)
  return totals


def _model_label(slug: str) -> str:
  # minor version is optional: single-number families (claude-fable-5) label as
  # just the major ("Fable 5")
  m = re.match(r'^claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?', slug)
  if m is None:
    return slug
  family, maj, minor = m.groups()
  version = maj if minor is None else f'{maj}.{minor}'
  return f'{family.title()} {version}'


def _version() -> str:
  # claude code exports AI_AGENT like "claude-code_2-1-181_agent"
  agent = os.environ.get('AI_AGENT')
  if agent is not None:
    m = re.match(r'claude-code_(\d+(?:-\d+)*)_', agent)
    if m is not None:
      return m.group(1).replace('-', '.')
  # fall back to a version-looking component of a versioned install path
  # (e.g. .../versions/2.1.181/claude)
  execpath = os.environ.get('CLAUDE_CODE_EXECPATH')
  if execpath is not None:
    for part in Path(execpath).parts:
      if re.match(r'^\d+\.\d+', part) is not None:
        return part
  return 'unknown'


# --- footer formatting + parsing --------------------------------------------


def _fmt_int(n: int) -> str:
  return f'{n:,}'.replace(',', _THOUSANDS)


def _to_labels(slug_counts: dict[str, Counts]) -> dict[str, Counts]:
  """collapse model-slug-keyed counts to the human labels the footer carries."""
  labels: dict[str, Counts] = {}
  for slug, c in slug_counts.items():
    label = _model_label(slug)
    labels[label] = _add(labels.get(label, _zero()), c)
  return labels


def _fmt_entry(label: str, c: Counts) -> str:
  return (
    f'{label}: {_UP} {_fmt_int(c.get("input", 0))} / {_fmt_int(c.get("cache_write", 0))} '
    f'({_fmt_int(c.get("cache_read", 0))}) {_DOWN} {_fmt_int(c.get("output", 0))}'
  )


def _format_footer(versions: list[str], label_counts: dict[str, Counts]) -> str:
  token_parts = ', '.join(_fmt_entry(m, label_counts[m]) for m in label_counts)
  return f'> created with Claude Code {", ".join(versions)} | {token_parts}'


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


@dataclass
class Footer:
  versions: list[str]
  delta: dict[str, Counts]  # label-keyed (the footer renders labels, not slugs)


def _unfmt(s: str) -> int:
  return int(s.replace(_THOUSANDS, ''))


def _parse_footer(commit_msg: str) -> Optional[Footer]:
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  delta: dict[str, Counts] = {}
  for chunk in m.group('tokens').split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    label = pm.group('model').strip()
    counts = {
      'input': _unfmt(pm.group('input')),
      'cache_write': _unfmt(pm.group('cache_write')),
      'cache_read': _unfmt(pm.group('cache_read')),
      'output': _unfmt(pm.group('output')),
    }
    delta[label] = _add(delta.get(label, _zero()), counts)
  if len(delta) == 0:
    return None
  versions = [v.strip() for v in m.group('versions').split(', ')]
  return Footer(versions=versions, delta=delta)


# --- per-session baseline state ---------------------------------------------


def _repo_root() -> Path:
  return Path(__file__).resolve().parents[1]


class State:
  """token baselines in <repo>/.token_accounting_state.json (per-worktree, gitignored).

  committed: model_slug -> per-class cumulative as of the last successful commit —
    the baseline the next delta subtracts.
  staged: model_slug -> per-class cumulative proposed by the generator for a commit
    not yet landed; the post-commit hook (--record) promotes it to committed, so a
    failed-then-retried commit reads an unchanged baseline.

  Keyed by model slug, not session id — the file is one-per-worktree and a worktree
  hosts a single session in practice.
  """

  def __init__(self, path: Path):
    self._path = path
    self.committed: dict[str, Counts] = {}
    self.staged: dict[str, Counts] = {}
    if path.exists():
      try:
        data = json.loads(path.read_text())
      except (OSError, json.JSONDecodeError):
        data = {}
      if isinstance(data, dict):
        self.committed = data.get('committed', {})
        self.staged = data.get('staged', {})

  def stage(self, cum: dict[str, Counts]) -> None:
    self.staged = cum
    self._save()

  def record(self) -> None:
    self.committed = self.staged
    self.staged = {}
    self._save()

  def _save(self) -> None:
    payload = json.dumps({'committed': self.committed, 'staged': self.staged}, indent=2)
    tmp = self._path.with_name(self._path.name + '.tmp')
    tmp.write_text(payload + '\n')
    tmp.replace(self._path)


# --- modes ------------------------------------------------------------------


def _emit_default(cum_now: dict[str, Counts], version: str, state: State) -> str:
  delta: dict[str, Counts] = {}
  for slug, cum in cum_now.items():
    delta[slug] = _sub(cum, _effective_baseline(state.committed.get(slug, _zero()), cum))
  footer = _format_footer([version], _to_labels(delta))
  state.stage(cum_now)
  return footer


def _emit_squash(
  commits: list[tuple[str, str]],
  land: Optional[dict[str, Counts]],
  version: str,
  state: State,
) -> tuple[str, list[str]]:
  """returns (footer, footerless_shas). delta accumulates per-class, label-keyed."""
  delta: dict[str, Counts] = {}
  versions: set[str] = set()
  footerless: list[str] = []
  for sha, body in commits:
    f = _parse_footer(body)
    if f is None:
      footerless.append(sha)
      continue
    for label, c in f.delta.items():
      delta[label] = _add(delta.get(label, _zero()), c)
    versions.update(f.versions)
  if land is not None:
    remainder: dict[str, Counts] = {}
    for slug, cum in land.items():
      remainder[slug] = _sub(cum, _effective_baseline(state.committed.get(slug, _zero()), cum))
    for label, c in _to_labels(remainder).items():
      delta[label] = _add(delta.get(label, _zero()), c)
    versions.add(version)
  return _format_footer(sorted(versions), delta), footerless


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


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='print/aggregate the Claude Code commit footer')
  group = parser.add_mutually_exclusive_group()
  group.add_argument(
    '--record',
    action='store_true',
    help='promote the staged cumulative to the committed baseline (post-commit hook)',
  )
  group.add_argument(
    '--squash',
    metavar='RANGE',
    help='emit an aggregated footer over a git range (for /land squash merges)',
  )
  args = parser.parse(argv)

  state = State(_repo_root() / STATE_FILENAME)

  if args['record'] is True:
    state.record()
    return 0

  if args['squash'] is not None:
    commits = _git_log(args['squash'])
    jsonl = _find_session_jsonl()
    land: Optional[dict[str, Counts]] = None
    if jsonl is not None:
      cum = _cumulative_usage(jsonl)
      if len(cum) > 0:
        land = cum
    if land is None:
      print(
        'warning: no land-session transcript/usage found; aggregating branch footers only',
        file=sys.stderr,
      )
    footer, footerless = _emit_squash(commits, land, _version(), state)
    if len(footerless) > 0:
      shas = ', '.join(s[:9] for s in footerless)
      print(
        f'warning: {len(footerless)} commit(s) without a parseable footer counted 0: {shas}',
        file=sys.stderr,
      )
    print(footer)
    return 0

  jsonl = _find_session_jsonl()
  if jsonl is None:
    print('error: no Claude Code session transcript found', file=sys.stderr)
    return 1
  cum = _cumulative_usage(jsonl)
  if len(cum) == 0:
    print(f'error: no assistant usage recorded yet in {jsonl.name}', file=sys.stderr)
    return 1
  print(_emit_default(cum, _version(), state))
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
