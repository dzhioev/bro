#!/usr/bin/env python3
"""prints a git commit footer crediting the token spend behind a commit.

The footer is the offline-readable cache of a session's token spend, attributed to
the commits it produced, so that summing a per-commit *delta* across `git log`
yields the true total for a range — and stays correct across squash merges.

The four billed token classes, the footer line shape, and the cumulative-usage
sources (a bro run's env-pointed usage file, the Claude Code session transcript)
are owned by the `llm.usage` module; this script owns the per-commit
delta/baseline machinery on top of `usage.current_usage()`.

Three modes:
- default: emit the footer for the commit about to be made. The per-class delta is
  this session's cumulative usage now minus the baseline already attributed to its
  earlier commits (read from the state file). Also stages the new cumulative for
  the post-commit hook to promote.
- --record: promote the staged cumulative to the committed baseline. Run by the
  post-commit git hook once a commit actually lands, so the mark only advances
  after a *successful* commit (retries and footerless commits stay correct).
- --squash <range>: emit an aggregated footer for a squash merge — the sum of
  every branch commit's per-class deltas / the union of agents plus the land
  session's uncommitted remainder. Run by /land and injected into the PR body, so
  the server-side squash commit carries the sum of the children it discards.

State lives in a gitignored `<repo>/.token_accounting_state.json`; git history
carries only the durable per-class deltas / agents. Runs through base.args, so
it needs the project venv active (the editable install puts `base` and `usage` on
the path even when invoked by file path); the post-commit hook surfaces a failure
rather than swallowing it, so committing without the venv is caught.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import llm.usage as usage
from base.args import Parser
from llm.usage import Counts
from workspace.paths import project_root

STATE_FILENAME = '.token_accounting_state.json'


def _effective_baseline(committed: Counts, cum: Counts) -> Counts:
  """the cumulative to subtract for a delta.

  Normally the committed mark. But within one session every class only grows, so a
  current cumulative that is smaller in any class means the usage source reset — a
  different session reused this worktree's state file. Treat the baseline as zero
  so the commit is credited its full new cumulative, not a bogus negative delta.
  """
  if any(cum.get(c, 0) < committed.get(c, 0) for c in usage.CLASSES):
    return usage.zero()
  return committed


# --- per-session baseline state ---------------------------------------------


def _repo_root() -> Path:
  return project_root()


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


def _emit_default(current: usage.Usage, state: State) -> str:
  delta: dict[str, Counts] = {}
  for slug, cum in current.per_model.items():
    delta[slug] = usage.subtract(
      cum, _effective_baseline(state.committed.get(slug, usage.zero()), cum)
    )
  footer = usage.format_footer([current.agent], usage.to_labels(delta))
  state.stage(current.per_model)
  return footer


def _emit_squash(
  commits: list[tuple[str, str]],
  land: Optional[usage.Usage],
  state: State,
) -> tuple[str, list[str]]:
  """returns (footer, footerless_shas). delta accumulates per-class, label-keyed."""
  delta: dict[str, Counts] = {}
  agents: set[str] = set()
  footerless: list[str] = []
  for sha, body in commits:
    f = usage.parse_footer(body)
    if f is None:
      footerless.append(sha)
      continue
    for label, c in f.delta.items():
      delta[label] = usage.add(delta.get(label, usage.zero()), c)
    agents.update(f.agents)
  if land is not None:
    remainder: dict[str, Counts] = {}
    for slug, cum in land.per_model.items():
      remainder[slug] = usage.subtract(
        cum, _effective_baseline(state.committed.get(slug, usage.zero()), cum)
      )
    for label, c in usage.to_labels(remainder).items():
      delta[label] = usage.add(delta.get(label, usage.zero()), c)
    agents.add(land.agent)
  return usage.format_footer(sorted(agents), delta), footerless


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
  parser = Parser(description='print/aggregate the token-accounting commit footer')
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
    land = usage.current_usage()
    if land is None:
      print(
        'warning: no land-session usage found; aggregating branch footers only',
        file=sys.stderr,
      )
    footer, footerless = _emit_squash(commits, land, state)
    if len(footerless) > 0:
      shas = ', '.join(s[:9] for s in footerless)
      print(
        f'warning: {len(footerless)} commit(s) without a parseable footer counted 0: {shas}',
        file=sys.stderr,
      )
    print(footer)
    return 0

  current = usage.current_usage()
  if current is None:
    print(
      f'error: no usage source found (no {usage.USAGE_FILE_VARIABLE} pointer, '
      'no Claude Code session transcript with usage)',
      file=sys.stderr,
    )
    return 1
  print(_emit_default(current, state))
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
