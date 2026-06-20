#!/usr/bin/env python3
"""prints a git commit footer crediting the Claude Code session(s) behind a commit.

The footer is the offline-readable cache of a session's token spend, attributed to
the commits it produced, so that summing a per-commit *delta* across `git log`
yields the true total for a range — and stays correct across squash merges. The
authoritative record is each session's recorded trail (keyed by session id); this
footer mirrors it into git history.

Three modes:
- default: emit the footer for the commit about to be made. The per-model token
  delta is this session's cumulative usage now minus the baseline already
  attributed to its earlier commits (read from the state file). Also stages the
  new cumulative for the post-commit hook to promote.
- --record: promote the staged cumulative to the committed baseline. Run by the
  post-commit git hook once a commit actually lands, so the mark only advances
  after a *successful* commit (retries and footerless commits stay correct).
- --squash <range>: emit an aggregated footer for a squash merge — the union of
  every branch commit's deltas / sessions / versions plus the land session's own
  uncommitted remainder. Run by /land and injected into the PR body, so the
  server-side squash commit carries the sum of the children it discards.

Footer shape (two `>`-quoted lines; `'` thousands separator so it never collides
with the `, ` joining model entries and list items):

  > created with Claude Code <versions> | <model>: <delta>[, <model>: <delta> …]
  > session(s): <session-id>[, <session-id> …]

State lives in a gitignored `<repo>/.token_accounting_state.json`; git history
carries only the durable deltas / versions / sessions. Kept stdlib-only so the
post-commit hook and /pr can run it without the project venv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STATE_FILENAME = '.token_accounting_state.json'
_THOUSANDS = "'"

# claude code labels locally-generated assistant turns (interrupts, local errors,
# injected notices) with this sentinel model — no real API round-trip, so their
# usage is not billed spend and must not be credited to any commit.
_SYNTHETIC_MODEL = '<synthetic>'

_USAGE_FIELDS = (
  'input_tokens',
  'cache_creation_input_tokens',
  'cache_read_input_tokens',
  'output_tokens',
)


# --- transcript reading -----------------------------------------------------


def _encode_cwd(cwd: str) -> str:
  return cwd.replace('/', '-').replace('.', '-')


def _find_session_jsonl() -> Path | None:
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


def _cumulative_usage(path: Path) -> dict[str, int]:
  """returns {model_slug: total_tokens} summed across every billed assistant message.

  synthetic turns (model `<synthetic>`) are skipped — they carry no real API spend.
  """
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
      if model == _SYNTHETIC_MODEL:
        continue
      total = sum(int(v) for k in _USAGE_FIELDS if (v := u.get(k)) is not None)
      totals[model] = totals.get(model, 0) + total
  return totals


def _model_label(slug: str) -> str:
  m = re.match(r'^claude-(opus|sonnet|haiku)-(\d+)-(\d+)', slug)
  if m is None:
    return slug
  family, maj, minor = m.groups()
  return f'{family.title()} {maj}.{minor}'


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


def _to_labels(slug_tokens: dict[str, int]) -> dict[str, int]:
  """collapse model-slug-keyed tokens to the human labels the footer carries."""
  labels: dict[str, int] = {}
  for slug, n in slug_tokens.items():
    label = _model_label(slug)
    labels[label] = labels.get(label, 0) + n
  return labels


def _format_footer(versions: list[str], label_tokens: dict[str, int], sessions: list[str]) -> str:
  token_parts = ', '.join(f'{m}: {_fmt_int(n)}' for m, n in label_tokens.items())
  return (
    f'> created with Claude Code {", ".join(versions)} | {token_parts}\n'
    f'> session(s): {", ".join(sessions)}'
  )


_FOOTER_RE = re.compile(
  r'^>\s*created with Claude Code\s+(?P<versions>.+?)\s*\|\s*(?P<tokens>.+?)\s*\n'
  r'>\s*session\(s\):\s*(?P<sessions>.+?)\s*$',
  re.MULTILINE,
)
_PART_RE = re.compile(r"^(?P<model>.*?):\s*(?P<n>[\d']+)$")


@dataclass
class Footer:
  versions: list[str]
  delta: dict[str, int]  # label-keyed (the footer renders labels, not slugs)
  sessions: list[str]


def _parse_footer(commit_msg: str) -> Footer | None:
  m = _FOOTER_RE.search(commit_msg)
  if m is None:
    return None
  delta: dict[str, int] = {}
  for chunk in m.group('tokens').split(', '):
    pm = _PART_RE.match(chunk.strip())
    if pm is None:
      continue
    model = pm.group('model').strip()
    delta[model] = delta.get(model, 0) + int(pm.group('n').replace(_THOUSANDS, ''))
  if len(delta) == 0:
    return None
  versions = [v.strip() for v in m.group('versions').split(', ')]
  sessions = [s.strip() for s in m.group('sessions').split(', ')]
  return Footer(versions=versions, delta=delta, sessions=sessions)


# --- per-session baseline state ---------------------------------------------


def _repo_root() -> Path:
  return Path(__file__).resolve().parents[1]


class State:
  """per-session token baselines in <repo>/.token_accounting_state.json.

  committed: session id -> {model_slug: cumulative} as of the session's last
    successful commit — the baseline the next delta subtracts.
  staged: session id -> {model_slug: cumulative} proposed by the generator for a
    commit not yet landed; the post-commit hook (--record) promotes it to
    committed, so a failed-then-retried commit reads an unchanged baseline.
  """

  def __init__(self, path: Path):
    self._path = path
    self.committed: dict[str, dict[str, int]] = {}
    self.staged: dict[str, dict[str, int]] = {}
    if path.exists():
      try:
        data = json.loads(path.read_text())
      except (OSError, json.JSONDecodeError):
        data = {}
      if isinstance(data, dict):
        self.committed = data.get('committed', {})
        self.staged = data.get('staged', {})

  def baseline(self, session: str) -> dict[str, int]:
    return self.committed.get(session, {})

  def stage(self, session: str, cum: dict[str, int]) -> None:
    self.staged[session] = cum
    self._save()

  def record(self) -> None:
    for session, cum in self.staged.items():
      self.committed[session] = cum
    self.staged = {}
    self._save()

  def _save(self) -> None:
    payload = json.dumps({'committed': self.committed, 'staged': self.staged}, indent=2)
    tmp = self._path.with_name(self._path.name + '.tmp')
    tmp.write_text(payload + '\n')
    tmp.replace(self._path)


# --- modes ------------------------------------------------------------------


def _emit_default(cum_now: dict[str, int], session: str, version: str, state: State) -> str:
  baseline = state.baseline(session)
  delta = {m: cum_now[m] - baseline.get(m, 0) for m in cum_now}
  footer = _format_footer([version], _to_labels(delta), [session])
  state.stage(session, cum_now)
  return footer


def _emit_squash(
  commits: list[tuple[str, str]],
  land: tuple[str, dict[str, int]] | None,
  version: str,
  state: State,
) -> tuple[str, list[str]]:
  """returns (footer, footerless_shas). delta accumulates label-keyed."""
  delta: dict[str, int] = {}
  sessions: set[str] = set()
  versions: set[str] = set()
  footerless: list[str] = []
  for sha, body in commits:
    f = _parse_footer(body)
    if f is None:
      footerless.append(sha)
      continue
    for m, n in f.delta.items():
      delta[m] = delta.get(m, 0) + n
    sessions.update(f.sessions)
    versions.update(f.versions)
  if land is not None:
    land_session, land_cum = land
    baseline = state.baseline(land_session)
    remainder = {m: land_cum[m] - baseline.get(m, 0) for m in land_cum}
    for m, n in _to_labels(remainder).items():
      delta[m] = delta.get(m, 0) + n
    sessions.add(land_session)
    versions.add(version)
  return _format_footer(sorted(versions), delta, sorted(sessions)), footerless


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


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description='print/aggregate the Claude Code commit footer')
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
  args = parser.parse_args(argv)

  state = State(_repo_root() / STATE_FILENAME)

  if args.record is True:
    state.record()
    return 0

  if args.squash is not None:
    commits = _git_log(args.squash)
    jsonl = _find_session_jsonl()
    land: tuple[str, dict[str, int]] | None = None
    if jsonl is not None:
      cum = _cumulative_usage(jsonl)
      if len(cum) > 0:
        land = (jsonl.stem, cum)
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
  print(_emit_default(cum, jsonl.stem, _version(), state))
  return 0


if __name__ == '__main__':
  sys.exit(main())
