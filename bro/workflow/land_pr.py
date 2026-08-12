#!/usr/bin/env python
"""merge the approved PR for the current branch in one shot.

Runs the deterministic tail of a dev session: resolve the PR, enforce the merge
preconditions, aggregate the branch's token-accounting footers, merge, and
delete the remote feature branch.

The branch lands as one commit by default — a server-side squash carrying the
PR's title and body. A branch that carries more than one logically separate
change lands as several instead, per the plan `--plan <path>` reads: one
`fold <sha> ...` line per landed commit, in landing order, naming the commits it
folds — together partitioning what the PR adds to its base — with that commit's
message written in the lines under it, or nothing to keep the message of the
first commit it folds. Folds need not be contiguous — a review fix at the tip
usually belongs to the first one — and the fold reorders the branch accordingly,
which is where it can conflict.

Landing several commits rewrites the branch (a non-interactive rebase folds each
one, then the chain is rebuilt so every landed commit carries the aggregated
footer of what it folds) and force-pushes it before a rebase merge. What makes
that safe is proven rather than assumed: the rewritten tip's tree must equal the
tree of the PR head it replaces, so the content under review reaches master
unchanged and only the commit boundaries move. The local branch may itself be a
restructuring of the PR head — splitting a commit that straddles two folds is
done there — since it is the content, not the history, that has to match.

Preconditions (each failure aborts with a message on stderr and exit 1):
- the PR for the current branch exists and is OPEN
- reviewDecision is APPROVED; `--no-review` waives a *missing* review, but
  CHANGES_REQUESTED is always refused
- the body has no unchecked `- [ ]` boxes unless `--allow-unchecked`
- every status check has concluded and passed. Pending checks are waited out
  (`--wait-checks` seconds) and then refuse the merge, as a failed check does.
  A PR with no checks passes. `--ignore-checks` drops the gate whole — no
  wait, no refusal, whatever the checks say.
- with `--plan`: the repository allows rebase merging, the worktree is clean,
  and the plan partitions the branch's commits

On success prints a single JSON object to stdout:

  {"pr": 310, "url": ..., "title": ..., "base": "master", "merged_sha": ...,
   "merged_at": "2026-07-03T10:41:02Z", "commits": 1, "branch_deleted": true}
"""

import contextlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import bro.llm.usage as usage
from bro.base import log, spawn
from bro.base.args import Parser
from bro.extra.github import api
from bro.workflow import commit_footer

__cli_name__ = 'land-pr'

_CHECK_POLL_INTERVAL = 15.0
_HEAD_POLL_INTERVAL = 2.0
_HEAD_POLL_BUDGET = 60.0

_SHA = r'[0-9a-fA-F]{4,40}'
_FOLD_HEADER = re.compile(rf'^fold[ \t]+({_SHA}(?:[ \t]+{_SHA})*)[ \t]*$')


class LandError(Exception):
  """a failed precondition or step; aborts the land with a clean message."""


def _run(command: list[str], *, capture: bool) -> str:
  """run a command with stderr passing through; return stripped stdout when capture."""
  stdout = subprocess.PIPE if capture else None
  result = spawn.run(command, stdout=stdout, text=True)
  if result.returncode != 0:
    raise LandError(f'`{" ".join(command[:3])}` failed with exit {result.returncode}')
  return result.stdout.strip() if capture else ''


def _git(*args: str, check: bool = True, env: Optional[dict[str, str]] = None) -> str:
  """run a git command capturing both streams; returns stripped stdout."""
  result = spawn.run(['git', *args], capture_output=True, text=True, env=env)
  if check and result.returncode != 0:
    raise LandError(f'`git {args[0]}` failed: {_output(result)}')
  return result.stdout.strip()


def _output(result: subprocess.CompletedProcess) -> str:
  """a command's captured streams as one message — git splits what it has to say
  across both."""
  return '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip() != '')


def _pr_view(fields: list[str], number: Optional[int] = None) -> dict[str, Any]:
  command = ['gh', 'pr', 'view']
  if number is not None:
    command.append(str(number))
  command += ['--json', ','.join(fields)]
  return json.loads(_run(command, capture=True))


def _unchecked_boxes(body: str) -> list[str]:
  return [m.group(1).strip() for m in re.finditer(r'^\s*[-*] \[ \] +(.+)$', body, re.MULTILINE)]


def _precondition_error(
  pr: dict[str, Any], no_review: bool, allow_unchecked: bool
) -> Optional[str]:
  if pr['state'] != 'OPEN':
    return f'PR #{pr["number"]} is {pr["state"]}, not OPEN'
  decision = pr['reviewDecision']
  if decision == 'CHANGES_REQUESTED':
    return f'PR #{pr["number"]} has changes requested; resolve the review before landing'
  if decision != 'APPROVED' and not no_review:
    shown = decision if decision != '' else 'none'
    return (
      f'PR #{pr["number"]} is not approved (reviewDecision={shown}); '
      'pass --no-review only when the user explicitly waived review'
    )
  unchecked = _unchecked_boxes(pr['body'])
  if len(unchecked) > 0 and not allow_unchecked:
    items = '\n'.join(f'  - [ ] {item}' for item in unchecked)
    return (
      f'PR #{pr["number"]} has unchecked test-plan boxes:\n{items}\n'
      'pass --allow-unchecked only when the user explicitly said to land anyway'
    )
  return None


def _check_name(entry: dict[str, Any]) -> str:
  # CheckRun carries `name`, the legacy StatusContext `context`
  return entry.get('name') or entry.get('context') or '(unnamed check)'


def _entry_state(entry: dict[str, Any]) -> str:
  if entry.get('__typename') == 'StatusContext':
    state = entry.get('state', '')
    # a status context has no separate status/conclusion pair: anything but
    # PENDING is a concluded state whose name doubles as the conclusion
    return 'pending' if state.lower() == 'pending' else api.check_state('completed', state)
  return api.check_state(entry.get('status'), entry.get('conclusion'))


def _split_checks(entries: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
  """(pending, failed) check names of a `statusCheckRollup` array."""
  pending = [_check_name(e) for e in entries if _entry_state(e) == 'pending']
  failed = [_check_name(e) for e in entries if _entry_state(e) == 'failed']
  return pending, failed


def _await_checks(number: int, entries: list[dict[str, Any]], wait_seconds: int) -> list[dict]:
  """the rollup once every check concluded, or once the wait budget is spent."""
  deadline = time.monotonic() + wait_seconds
  while True:
    pending, _ = _split_checks(entries)
    if len(pending) == 0 or time.monotonic() >= deadline:
      return entries
    log.info(f'waiting for {len(pending)} pending check(s): {", ".join(pending)}')
    time.sleep(_CHECK_POLL_INTERVAL)
    entries = _pr_view(['statusCheckRollup'], number=number).get('statusCheckRollup') or []


def _checks_error(number: int, entries: list[dict[str, Any]]) -> Optional[str]:
  pending, failed = _split_checks(entries)
  if len(failed) > 0:
    return f'PR #{number} has failing checks: {", ".join(failed)}; fix them or re-run them'
  if len(pending) > 0:
    return (
      f'PR #{number} still has pending checks: {", ".join(pending)}; '
      're-run land-pr once they conclude'
    )
  return None


def _log_ignored_checks(entries: list[dict[str, Any]]) -> None:
  pending, failed = _split_checks(entries)
  ignored = [*failed, *pending]
  if len(ignored) > 0:
    log.warning(
      f'--ignore-checks: merging past {len(failed)} failing and {len(pending)} '
      f'pending check(s): {", ".join(ignored)}'
    )


@dataclass(frozen=True)
class Fold:
  """one landed commit: the branch commits folded into it, and its message."""

  commits: tuple[str, ...]
  message: str


def _branch_commits(base: str) -> list[str]:
  """the commits the PR adds to its base, oldest first."""
  _git('fetch', '--no-tags', 'origin', f'+refs/heads/{base}:refs/remotes/origin/{base}')
  return _git('rev-list', '--reverse', f'origin/{base}..HEAD').split()


def _resolve(sha: str) -> Optional[str]:
  full = _git('rev-parse', '--verify', '--quiet', f'{sha}^{{commit}}', check=False)
  return full if full != '' else None


def _parse_plan(text: str, path: str) -> list[tuple[list[str], Optional[str]]]:
  """the plan's blocks: each `fold` line's shas with the message written under
  it, None where the block carries none.

  A line counts as a header only when every token after `fold` is a hex sha, so
  a message line that opens with the word passes through as the text it is.
  """
  blocks: list[tuple[list[str], list[str]]] = []
  for line in text.splitlines():
    header = _FOLD_HEADER.match(line)
    if header is not None:
      blocks.append((header.group(1).split(), []))
    elif len(blocks) > 0:
      blocks[-1][1].append(line)
    elif line.strip() != '':
      raise LandError(f'{path}: expected a `fold <sha> ...` line, found: {line.strip()}')
  if len(blocks) == 0:
    raise LandError(f'{path}: no `fold <sha> ...` line — the plan names one per landed commit')
  return [(shas, '\n'.join(message).strip() or None) for shas, message in blocks]


def _load_plan(path: str, branch: Sequence[str]) -> list[Fold]:
  """the landing plan at `path`, validated to partition `branch` exactly.

  Fold order is the order the commits land in; within a fold the branch's own
  order is kept, a fold having no order of its own.
  """
  try:
    text = Path(path).read_text()
  except OSError as error:
    raise LandError(f'cannot read the landing plan {path}: {error}') from error
  blocks = _parse_plan(text, path)
  if len(blocks) < 2:
    raise LandError(f'{path}: one fold is the plain squash — run land-pr without --plan')
  position = {sha: index for index, sha in enumerate(branch)}
  claimed: dict[str, int] = {}
  folds: list[Fold] = []
  for number, (shas, message) in enumerate(blocks, start=1):
    folded: list[str] = []
    for sha in shas:
      full = _resolve(sha)
      if full is None:
        raise LandError(f'{path}: fold {number} names {sha}, which is not a commit')
      if full not in position:
        raise LandError(f'{path}: fold {number} names {sha}, which the PR does not add to its base')
      if full in claimed:
        raise LandError(f'{path}: {sha} is in fold {claimed[full]} and fold {number} both')
      claimed[full] = number
      folded.append(full)
    folded.sort(key=position.__getitem__)
    if message is None:
      message = usage.strip_footer(_git('log', '-1', '--format=%B', folded[0]))
    folds.append(Fold(commits=tuple(folded), message=message.strip()))
  unclaimed = [sha for sha in branch if sha not in claimed]
  if len(unclaimed) > 0:
    shas = ', '.join(sha[:9] for sha in unclaimed)
    raise LandError(f'{path}: the plan leaves {len(unclaimed)} branch commit(s) unlanded: {shas}')
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
    raise LandError(f'the fold does not apply — grouping reorders the branch:\n{_output(result)}')
  landed = _git('rev-list', '--reverse', f'{base}..HEAD').split()
  if len(landed) != len(folds):
    raise LandError(f'the rebase produced {len(landed)} commit(s) for {len(folds)} fold(s)')
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
  """put the branch back on `head` if the block fails — a rewrite that never
  reached the remote must not survive as the session's local state."""
  try:
    yield
  except BaseException:
    _git('reset', '--hard', head, check=False)
    raise


def _await_head(number: int, expected: str) -> None:
  """block until the PR reports `expected` as its head: GitHub's view of a push
  lags it by moments, and the merge must not race that."""
  deadline = time.monotonic() + _HEAD_POLL_BUDGET
  while _pr_view(['headRefOid'], number=number)['headRefOid'] != expected:
    if time.monotonic() >= deadline:
      raise LandError(f'PR #{number} does not report the pushed head {expected[:9]} yet')
    time.sleep(_HEAD_POLL_INTERVAL)


def _body_with_footer(body: str, footer: str) -> str:
  trimmed = body.rstrip()
  if footer == '':
    return trimmed
  if trimmed == '':
    return footer
  return f'{trimmed}\n\n{footer}'


def _delete_remote_branch(branch: str) -> bool:
  """delete only the remote ref — the local branch and checkout stay untouched
  (deleting them out from under a live worktree is the session manager's call).
  The merge is already done when this runs, so a failure degrades to a warning."""
  result = spawn.run(['git', 'push', 'origin', '--delete', branch], text=True)
  if result.returncode != 0:
    log.warning(f'could not delete remote branch {branch} (exit {result.returncode})')
    return False
  return True


_PR_FIELDS = [
  'number',
  'title',
  'body',
  'state',
  'reviewDecision',
  'baseRefName',
  'headRefName',
  'headRefOid',
  'url',
  'statusCheckRollup',
]


def _squash_merge(pr: dict[str, Any], footer: str) -> None:
  """let GitHub fold the branch into one commit carrying the PR's own text."""
  _run(
    [
      'gh',
      'pr',
      'merge',
      str(pr['number']),
      '--squash',
      '--subject',
      pr['title'],
      '--body',
      _body_with_footer(pr['body'], footer),
    ],
    capture=False,
  )


def _rebase_merge(
  pr: dict[str, Any],
  folds: Sequence[Fold],
  footers: Sequence[str],
  no_review: bool,
  allow_unchecked: bool,
) -> None:
  """land the folds as commits of their own: fold locally, prove the result is
  the reviewed content, publish it, and let GitHub replay it onto the base."""
  number, head_ref = pr['number'], pr['headRefName']
  allowed = _run(['gh', 'api', 'repos/{owner}/{repo}', '--jq', '.allow_rebase_merge'], capture=True)
  if allowed != 'true':
    raise LandError('the repository disallows rebase merging, so the branch can only land squashed')
  if _git('status', '--porcelain', '--untracked-files=no') != '':
    raise LandError('the worktree has uncommitted changes; the fold rewrites the branch in place')
  _git('fetch', '--no-tags', 'origin', f'+refs/heads/{head_ref}:refs/remotes/origin/{head_ref}')
  reviewed = _git('rev-parse', f'refs/remotes/origin/{head_ref}')
  local = _git('rev-parse', 'HEAD')
  base = _git('merge-base', f'origin/{pr["baseRefName"]}', 'HEAD')
  with _branch_restored(local):
    tip = _rewrite(folds, base, footers)
    if _git('rev-parse', f'{tip}^{{tree}}') != _git('rev-parse', f'{reviewed}^{{tree}}'):
      raise LandError(
        f'the fold changes the content of PR head {reviewed[:9]}, which review and '
        'the checks passed on; nothing was pushed'
      )
    _git('reset', '--hard', tip)
    _git(
      'push', f'--force-with-lease={head_ref}:{reviewed}', 'origin', f'{tip}:refs/heads/{head_ref}'
    )
  if any(len(footer) > 0 for footer in footers):
    commit_footer.record_session_spend()
  _await_head(number, tip)
  # the push is new head commits as far as GitHub is concerned, so a repository
  # that dismisses approvals on a push has just dismissed this one
  error = _precondition_error(_pr_view(_PR_FIELDS, number=number), no_review, allow_unchecked)
  if error is not None:
    raise LandError(
      f'{error}\nthe folded branch is pushed and unchanged in content; re-run once this clears'
    )
  _run(['gh', 'pr', 'merge', str(number), '--rebase', '--match-head-commit', tip], capture=False)


def _land(
  no_review: bool,
  allow_unchecked: bool,
  ignore_checks: bool,
  wait_checks: int,
  plan: Optional[str],
) -> dict:
  pr = _pr_view(_PR_FIELDS)
  error = _precondition_error(pr, no_review, allow_unchecked)
  if error is not None:
    raise LandError(error)
  rollup = pr.get('statusCheckRollup') or []
  if ignore_checks:
    _log_ignored_checks(rollup)
  else:
    # after the cheap preconditions: a PR that cannot merge anyway must not
    # cost the check wait
    rollup = _await_checks(pr['number'], rollup, wait_checks)
    error = _checks_error(pr['number'], rollup)
    if error is not None:
      raise LandError(error)
  branch = _branch_commits(pr['baseRefName'])
  folds = _load_plan(plan, branch) if plan is not None else None
  grouped = [fold.commits for fold in folds] if folds is not None else [tuple(branch)]
  footers = commit_footer.group_footers(grouped)
  if folds is None:
    _squash_merge(pr, footers[0])
    if footers[0] != '':
      commit_footer.record_session_spend()
  else:
    _rebase_merge(pr, folds, footers, no_review, allow_unchecked)
  merged = _pr_view(['state', 'mergeCommit', 'mergedAt'], number=pr['number'])
  if merged['state'] != 'MERGED':
    raise LandError(f'merge command succeeded but PR state is {merged["state"]}')
  return {
    'pr': pr['number'],
    'url': pr['url'],
    'title': pr['title'],
    'base': pr['baseRefName'],
    'merged_sha': merged['mergeCommit']['oid'],
    'merged_at': merged['mergedAt'],
    'commits': len(grouped),
    'branch_deleted': _delete_remote_branch(pr['headRefName']),
  }


def land_pr(
  no_review: bool,
  allow_unchecked: bool,
  ignore_checks: bool,
  wait_checks: int,
  plan: Optional[str],
) -> Optional[int]:
  try:
    result = _land(no_review, allow_unchecked, ignore_checks, wait_checks, plan)
  except LandError as error:
    log.error(str(error))
    return 1
  print(json.dumps(result))
  return None


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='merge the approved PR for the current branch in one shot')
  parser.add_argument(
    '--plan',
    metavar='PATH',
    help='land the branch as several commits, grouped by this JSON plan (default: one squash)',
  )
  parser.add_argument(
    '--no-review',
    action='store_true',
    help='merge without an APPROVED review (explicit user waiver; changes-requested still refuses)',
  )
  parser.add_argument(
    '--allow-unchecked',
    action='store_true',
    help='merge despite unchecked test-plan boxes (explicit user waiver)',
  )
  parser.add_argument(
    '--ignore-checks',
    action='store_true',
    help='merge whatever the status checks say, pending or failed (explicit user waiver)',
  )
  parser.add_argument(
    '--wait-checks',
    type=int,
    default=480,
    metavar='SECONDS',
    help='how long to wait for pending checks to conclude before refusing (0 to refuse at once)',
  )
  return land_pr(**parser.parse(argv))
