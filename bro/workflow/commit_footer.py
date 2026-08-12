#!/usr/bin/env python3
"""maintains the git commit footer crediting the token spend behind a commit.

The footer is the offline-readable cache of a session's token spend, attributed to
the commits it produced, so that summing a per-commit *delta* across `git log`
yields the true total for a range — and stays correct across squash merges.

The four billed token classes, the footer line shape, and the cumulative-usage
sources (a bro run's env-pointed usage file, the Claude Code session transcript)
are owned by the `bro.llm.usage` module; this script owns the per-commit
delta/baseline machinery on top of `usage.current_usage()`.

The footer is applied by git, not by the agent: `install_hooks` puts the two
packaged hooks (`hooks/`) into a repository, and every commit made with an agent
usage source in the environment carries the footer with no session involvement.
A process without a usage source — a human's shell — is a no-op for both hooks.

Four CLI modes:
- --append <msg-file>: run by the commit-msg git hook. No env-keyed usage source
  (`BRO_USAGE_FILE` / `CLAUDE_CODE_SESSION_ID` — a human's shell carries
  neither), an empty message, or a message already carrying a parseable footer
  (an amend, a reword — the commit keeps its original attribution) leaves the
  file untouched; otherwise the delta footer is appended and the new cumulative
  staged. A failure fails the commit, surfacing at the moment of the mistake.
- --record: promote the staged cumulative to the committed baseline. Run by the
  post-commit git hook once a commit actually lands, so the mark only advances
  after a *successful* commit (retries stay correct), and only when something is
  staged (a footerless commit leaves the baseline alone).
- --squash <range>: emit the aggregated footer for the range's commits — what a
  squash merge's single commit carries, the sum of the children it discards. A
  range with no footered commits emits nothing, so the caller needs no
  accounting switch of its own.
- default: print the footer the next commit would carry (also staging its
  cumulative, exactly as --append would).

and the fold aggregation, `group_footers(groups)`: the footer each commit of a
rewritten history carries — the sum of the per-class deltas / the union of
agents of the commits folded into it, plus the current session's uncommitted
remainder on the last group that carries accounting, so a fold conserves the
spend its commits accounted for however they are grouped. A set of commits with
no footers between them aggregates to nothing, so the caller needs no accounting
switch of its own. `record_session_spend()` promotes that remainder to the
baseline once the footers are published, so it is credited exactly once.

State lives in a gitignored `<repo>/.token_accounting_state.json`; git history
carries only the durable per-class deltas / agents. Runs through bro.base.args, so
it needs the project venv active (the editable install puts `base` and `usage` on
the path even when invoked by file path); the hooks surface a failure rather than
swallowing it, so committing without the venv is caught.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

import bro.llm.usage as usage
from bro.base import log
from bro.base.args import Parser
from bro.llm.usage import Counts
from bro.workspace.git import git_out

__cli_name__ = 'commit-footer'

STATE_FILENAME = '.token_accounting_state.json'
HOOK_NAMES = ('commit-msg', 'post-commit')


def install_hooks(repository: Path, *, overwrite: bool = True) -> None:
  """copy the packaged footer hooks into `repository`'s git hooks directory.

  overwrite=False leaves an existing hook file alone — for the session-start
  provisioning path, which must not clobber hooks a repo installed itself;
  the explicit installer keeps the refreshing default.
  """
  root = Path(git_out('rev-parse', '--show-toplevel', cwd=str(repository)))
  hooks_path = Path(git_out('rev-parse', '--git-path', 'hooks', cwd=str(root)))
  if not hooks_path.is_absolute():
    hooks_path = root / hooks_path
  hooks_path.mkdir(parents=True, exist_ok=True)
  for hook_name in HOOK_NAMES:
    destination = hooks_path / hook_name
    if not overwrite and destination.exists():
      continue
    resource = importlib.resources.files('bro.workflow').joinpath(f'hooks/{hook_name}')
    with importlib.resources.as_file(resource) as source:
      shutil.copyfile(source, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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
  return Path(git_out('rev-parse', '--show-toplevel')).resolve()


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
    # a commit that generated no footer (a human's, or --append leaving an
    # already-footered message) stages nothing; promoting would wipe the
    # baseline and over-credit the next commit.
    if len(self.staged) == 0:
      return
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


def _append(message_path: Path, state: State) -> None:
  # agenthood is the presence of an env-keyed usage source — deliberately not
  # current_usage()'s working-directory transcript fallback, which in a human's
  # shell would resolve a past session's transcript and footer their commit
  if (
    os.environ.get(usage.USAGE_FILE_VARIABLE) is None
    and os.environ.get(usage.SESSION_ID_VARIABLE) is None
  ):
    return
  current = usage.current_usage()
  if current is None:
    return
  message = message_path.read_text()
  if len(message.strip()) == 0 or usage.parse_footer(message) is not None:
    return
  footer = _emit_default(current, state)
  message_path.write_text(f'{message.rstrip()}\n\n{footer}\n')


def _aggregate(
  commits: list[tuple[str, str]],
  session: Optional[usage.Usage],
  state: State,
) -> str:
  """the footer summing the commits' own deltas per class, label-keyed, plus the
  session's uncommitted remainder when one is given."""
  delta: dict[str, Counts] = {}
  agents: set[str] = set()
  for _, body in commits:
    parsed = usage.parse_footer(body)
    if parsed is None:
      continue
    for label, c in parsed.delta.items():
      delta[label] = usage.add(delta.get(label, usage.zero()), c)
    agents.update(parsed.agents)
  if session is not None:
    remainder: dict[str, Counts] = {}
    for slug, cum in session.per_model.items():
      remainder[slug] = usage.subtract(
        cum, _effective_baseline(state.committed.get(slug, usage.zero()), cum)
      )
    for label, c in usage.to_labels(remainder).items():
      delta[label] = usage.add(delta.get(label, usage.zero()), c)
    agents.add(session.agent)
  return usage.format_footer(sorted(agents), delta)


def _commit_messages(shas: Sequence[str]) -> list[tuple[str, str]]:
  """returns [(commit_sha, commit_message)] for the named commits, in the order given."""
  if len(shas) == 0:
    return []
  out = subprocess.check_output(
    ['git', 'log', '--no-walk=unsorted', '--pretty=format:%H%x1f%B%x1e', *shas], text=True
  )
  commits: list[tuple[str, str]] = []
  for record in out.split('\x1e'):
    record = record.strip()
    if len(record) == 0:
      continue
    sha, _, body = record.partition('\x1f')
    commits.append((sha, body))
  return commits


def _range_commits(git_range: str) -> list[str]:
  return subprocess.check_output(['git', 'rev-list', git_range], text=True).split()


def group_footers(groups: Sequence[Sequence[str]]) -> list[str]:
  """the footer for each group of commits folded into one, in group order.

  The current session's own remainder rides the last group with accounting to
  carry it, so it is credited once however the commits are grouped. Groups whose
  commits carry no footer aggregate to an empty string — as do all of them when
  the whole set is unaccounted, since footerless commits are an anomaly only
  where footers exist at all.
  """
  folded = [_commit_messages(group) for group in groups]
  accounted = {
    index
    for index, commits in enumerate(folded)
    if any(usage.parse_footer(body) is not None for _, body in commits)
  }
  if len(accounted) == 0:
    return ['' for _ in groups]
  footerless = [
    sha for commits in folded for sha, body in commits if usage.parse_footer(body) is None
  ]
  if len(footerless) > 0:
    shas = ', '.join(sha[:9] for sha in footerless)
    log.warning(f'{len(footerless)} commit(s) without a parseable footer counted 0: {shas}')
  session = usage.current_usage()
  if session is None:
    log.warning('no session usage found; aggregating the folded footers only')
  state = State(_repo_root() / STATE_FILENAME)
  last_accounted = max(accounted)
  return [
    _aggregate(commits, session if index == last_accounted else None, state)
    if index in accounted
    else ''
    for index, commits in enumerate(folded)
  ]


def record_session_spend() -> None:
  """promote the session's cumulative to the committed baseline, once a footer
  carrying its remainder has been published — a later commit or land in the same
  session is credited only what it spends after this point."""
  session = usage.current_usage()
  if session is None:
    return
  state = State(_repo_root() / STATE_FILENAME)
  state.stage(session.per_model)
  state.record()


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='maintain/aggregate the token-accounting commit footer')
  group = parser.add_mutually_exclusive_group()
  group.add_argument(
    '--append',
    metavar='MSG_FILE',
    help='append the footer to the proposed commit message (commit-msg hook)',
  )
  group.add_argument(
    '--record',
    action='store_true',
    help='promote the staged cumulative to the committed baseline (post-commit hook)',
  )
  group.add_argument(
    '--squash',
    metavar='RANGE',
    help='emit the aggregated footer over a git range (for a squash merge)',
  )
  args = parser.parse(argv)

  state = State(_repo_root() / STATE_FILENAME)

  if args['append'] is not None:
    _append(Path(args['append']), state)
    return 0

  if args['record'] is True:
    state.record()
    return 0

  if args['squash'] is not None:
    footer = group_footers([_range_commits(args['squash'])])[0]
    if footer != '':
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
