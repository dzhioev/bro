#!/usr/bin/env python
"""fold the branch into the commits it should land as.

The working commits a session accumulates — checkpoints, follow-up fixes — are
folded in place into the sequence the branch should carry, each landed commit
stamped with the token-accounting footer aggregated from what it folds plus the
session's spend since the last fold.

The plan `--plan <path>` reads names the landed commits: one `fold <sha> ...`
line each, in landing order, naming the commits it folds — together
partitioning what the branch adds to its base — with that commit's message
written in the lines under it, or nothing to keep the message of the first
commit it folds. One line may name no commits at all: it takes every commit the
others leave, so folding the whole branch into one commit is a plan of a single
`fold` line, and a plan survives a review round that appends to the branch.

Folds need not be contiguous — a review fix at the tip usually belongs to the
first one — and the fold reorders the branch accordingly, which is where it can
conflict. What it must not do is change content: the rewritten tip's tree has to
equal the tree of the branch it replaces, or the branch is left as it was.

On success prints a single JSON object to stdout:

  {"base": "master", "commits": [{"sha": ..., "subject": "..."}]}
"""

import contextlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import bro.llm.usage as usage
from bro.base import log, spawn
from bro.base.args import Parser
from bro.workflow import commit_footer

__cli_name__ = 'fold-branch'

_SHA = r'[0-9a-fA-F]{4,40}'
_FOLD_HEADER = re.compile(rf'^fold(?:[ \t]+({_SHA}(?:[ \t]+{_SHA})*))?[ \t]*$')


class FoldError(Exception):
  """a failed precondition or step; aborts the fold with a clean message."""


def _git(*args: str, check: bool = True, env: Optional[dict[str, str]] = None) -> str:
  """run a git command capturing both streams; returns stripped stdout."""
  result = spawn.run(['git', *args], capture_output=True, text=True, env=env)
  if check and result.returncode != 0:
    raise FoldError(f'`git {args[0]}` failed: {_output(result)}')
  return result.stdout.strip()


def _output(result: subprocess.CompletedProcess) -> str:
  """a command's captured streams as one message — git splits what it has to say
  across both."""
  return '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip() != '')


@dataclass(frozen=True)
class Fold:
  """one landed commit: the branch commits folded into it, and its message."""

  commits: tuple[str, ...]
  message: str


def _branch_commits(base: str) -> list[str]:
  """the commits the branch adds to `base`, oldest first."""
  _git('fetch', '--no-tags', 'origin', f'+refs/heads/{base}:refs/remotes/origin/{base}')
  return _git('rev-list', '--reverse', f'origin/{base}..HEAD').split()


def _resolve(sha: str) -> Optional[str]:
  full = _git('rev-parse', '--verify', '--quiet', f'{sha}^{{commit}}', check=False)
  return full if full != '' else None


def _parse_plan(text: str, path: str) -> list[tuple[list[str], Optional[str]]]:
  """the plan's blocks: each `fold` line's shas — empty where it names none —
  with the message written under it, None where the block carries none.

  A line counts as a header only when every token after `fold` is a hex sha, so
  a message line that opens with the word passes through as the text it is.
  """
  blocks: list[tuple[list[str], list[str]]] = []
  for line in text.splitlines():
    header = _FOLD_HEADER.match(line)
    if header is not None:
      blocks.append(((header.group(1) or '').split(), []))
    elif len(blocks) > 0:
      blocks[-1][1].append(line)
    elif line.strip() != '':
      raise FoldError(f'{path}: expected a `fold <sha> ...` line, found: {line.strip()}')
  if len(blocks) == 0:
    raise FoldError(f'{path}: no `fold <sha> ...` line — the plan names one per landed commit')
  return [(shas, '\n'.join(message).strip() or None) for shas, message in blocks]


def _claimed(
  blocks: Sequence[tuple[list[str], Optional[str]]], position: dict[str, int], path: str
) -> list[list[str]]:
  """each block's branch commits, the block naming none taking what the rest leave."""
  claimed: dict[str, int] = {}
  named: list[list[str]] = []
  for number, (shas, _) in enumerate(blocks, start=1):
    folded: list[str] = []
    for sha in shas:
      full = _resolve(sha)
      if full is None:
        raise FoldError(f'{path}: fold {number} names {sha}, which is not a commit')
      if full not in position:
        raise FoldError(f'{path}: fold {number} names {sha}, which the branch does not add')
      if full in claimed:
        raise FoldError(f'{path}: {sha} is in fold {claimed[full]} and fold {number} both')
      claimed[full] = number
      folded.append(full)
    named.append(folded)
  rest = [sha for sha in position if sha not in claimed]
  catch_all = [number for number, (shas, _) in enumerate(blocks, start=1) if len(shas) == 0]
  if len(catch_all) > 1:
    numbers = ', '.join(str(number) for number in catch_all)
    raise FoldError(f'{path}: folds {numbers} each name no commits; only one can take the rest')
  if len(catch_all) == 0:
    if len(rest) > 0:
      shas = ', '.join(sha[:9] for sha in rest)
      raise FoldError(f'{path}: the plan leaves {len(rest)} branch commit(s) unlanded: {shas}')
  elif len(rest) == 0:
    raise FoldError(f'{path}: fold {catch_all[0]} names no commits and the rest are all claimed')
  else:
    named[catch_all[0] - 1] = rest
  return named


def _load_plan(path: str, branch: Sequence[str]) -> list[Fold]:
  """the landing plan at `path`, validated to partition `branch` exactly.

  Fold order is the order the commits land in; within a fold the branch's own
  order is kept, a fold having no order of its own.
  """
  try:
    text = Path(path).read_text()
  except OSError as error:
    raise FoldError(f'cannot read the landing plan {path}: {error}') from error
  blocks = _parse_plan(text, path)
  position = {sha: index for index, sha in enumerate(branch)}
  folds: list[Fold] = []
  for folded, (_, message) in zip(_claimed(blocks, position, path), blocks, strict=True):
    folded.sort(key=position.__getitem__)
    if message is None:
      message = usage.strip_footer(_git('log', '-1', '--format=%B', folded[0]))
    folds.append(Fold(commits=tuple(folded), message=message.strip()))
  return folds


def _authorship(sha: str) -> dict[str, str]:
  """the environment that stamps a fold with the authorship of the commit it
  stands for — the landed commit is dated when the work happened, and a re-run
  of the same plan rebuilds the same commits rather than churning the branch."""
  name, email, authored, committed = _git(
    'log', '-1', '--format=%an%x1f%ae%x1f%aI%x1f%cI', sha
  ).split('\x1f')
  return {
    **os.environ,
    'GIT_AUTHOR_NAME': name,
    'GIT_AUTHOR_EMAIL': email,
    'GIT_AUTHOR_DATE': authored,
    'GIT_COMMITTER_DATE': committed,
  }


def _fold_todo(folds: Sequence[Fold]) -> str:
  lines: list[str] = []
  for fold in folds:
    lines.append(f'pick {fold.commits[0]}')
    lines += [f'fixup {sha}' for sha in fold.commits[1:]]
  return '\n'.join(lines) + '\n'


def _rewrite(folds: Sequence[Fold], base: str, footers: Sequence[str]) -> str:
  """fold the branch into one commit per fold and return the new tip.

  The rebase does the content work; the chain is then rebuilt with `commit-tree`
  to carry the landed messages, because a fixup keeps the first commit's message
  and discards the folded commits' accounting footers along with their text.
  """
  with tempfile.NamedTemporaryFile('w', suffix='.todo') as todo:
    todo.write(_fold_todo(folds))
    todo.flush()
    result = spawn.run(
      ['git', 'rebase', '--interactive', base],
      capture_output=True,
      text=True,
      env={
        **os.environ,
        'GIT_SEQUENCE_EDITOR': f'cp {shlex.quote(todo.name)}',
        'GIT_EDITOR': 'true',
      },
    )
  if result.returncode != 0:
    _git('rebase', '--abort', check=False)
    raise FoldError(f'the fold does not apply — grouping reorders the branch:\n{_output(result)}')
  landed = _git('rev-list', '--reverse', f'{base}..HEAD').split()
  if len(landed) != len(folds):
    raise FoldError(f'the rebase produced {len(landed)} commit(s) for {len(folds)} fold(s)')
  tip = base
  for sha, fold, footer in zip(landed, folds, footers, strict=True):
    message = fold.message if footer == '' else f'{fold.message}\n\n{footer}'
    tip = _git(
      'commit-tree',
      f'{sha}^{{tree}}',
      '-p',
      tip,
      '-m',
      message,
      env=_authorship(fold.commits[0]),
    )
  return tip


@contextlib.contextmanager
def _branch_restored(head: str) -> Generator[None]:
  """put the branch back on `head` if the block fails — a rewrite the branch was
  not left holding must not survive as the session's local state."""
  try:
    yield
  except BaseException:
    _git('reset', '--hard', head, check=False)
    raise


def _shaped(base: str) -> list[dict[str, str]]:
  """the branch's commits over `base`, oldest first, as sha and subject."""
  log_lines = _git('log', '--reverse', '--format=%H%x1f%s', f'{base}..HEAD').split('\n')
  return [
    {'sha': sha, 'subject': subject}
    for sha, _, subject in (line.partition('\x1f') for line in log_lines)
  ]


def _fold(plan: str, base: str) -> dict:
  if _git('status', '--porcelain', '--untracked-files=no') != '':
    raise FoldError('the worktree has uncommitted changes; the fold rewrites the branch in place')
  branch = _branch_commits(base)
  if len(branch) == 0:
    raise FoldError(f'the branch adds no commits to {base}; there is nothing to fold')
  folds = _load_plan(plan, branch)
  footers = commit_footer.group_footers([fold.commits for fold in folds])
  head = _git('rev-parse', 'HEAD')
  merge_base = _git('merge-base', f'origin/{base}', 'HEAD')
  with _branch_restored(head):
    tip = _rewrite(folds, merge_base, footers)
    if _git('rev-parse', f'{tip}^{{tree}}') != _git('rev-parse', f'{head}^{{tree}}'):
      raise FoldError('the fold changes the content of the branch; nothing was written')
    _git('reset', '--hard', tip)
  if any(len(footer) > 0 for footer in footers):
    commit_footer.record_session_spend()
  return {'base': base, 'commits': _shaped(merge_base)}


def fold_branch(plan: str, base: str) -> Optional[int]:
  try:
    result = _fold(plan, base)
  except FoldError as error:
    log.error(str(error))
    return 1
  print(json.dumps(result))
  return None


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='fold the branch into the commits it should land as')
  parser.add_argument(
    '--plan',
    required=True,
    metavar='PATH',
    help='the plan naming the landed commits, one `fold <sha> ...` line each',
  )
  parser.add_argument(
    '--base',
    default='master',
    metavar='BRANCH',
    help='the branch this one lands on (default: master)',
  )
  return fold_branch(**parser.parse(argv))
