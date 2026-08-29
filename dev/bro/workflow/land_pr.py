#!/usr/bin/env python
"""merge the approved PR for the current branch in one shot.

Runs the deterministic tail of a dev session: resolve the PR, enforce the merge
preconditions, rebase-merge, and delete the remote feature branch.

The branch lands as the commits it carries: nothing here rewrites it, so what
was approved is what reaches master, commit boundaries and accounting footers
included.

Preconditions (each failure aborts with a message on stderr and exit 1):
- the PR for the current branch exists and is OPEN
- reviewDecision is APPROVED; `--no-review` waives a *missing* review, but
  CHANGES_REQUESTED is always refused, and so is REVIEW_REQUIRED — that one is
  the base branch's own rule, which no flag here reaches
- the body has no unchecked `- [ ]` boxes unless `--allow-unchecked`
- the worktree is on the commit the PR carries, so the branch holds nothing the
  review never saw
- the repository allows rebase merging
- every status check has concluded and passed. Pending checks are waited out
  (`--wait-checks` seconds) and then refuse the merge, as a failed check does.
  A PR with no checks passes. `--ignore-checks` drops the gate whole — no
  wait, no refusal, whatever the checks say.

On success prints a single JSON object to stdout:

  {"pr": 310, "url": ..., "title": ..., "base": "master", "merged_sha": ...,
   "merged_at": "2026-07-03T10:41:02Z", "commits": 1, "branch_deleted": true}
"""

import json
import re
import subprocess
import time
from typing import Any, Optional

from bro.base import log, spawn
from bro.base.args import Parser
from bro.extra.github import api
from bro.workspace.git import git_out

__cli_name__ = 'land-pr'

_CHECK_POLL_INTERVAL = 15.0


class LandError(Exception):
  """a failed precondition or step; aborts the land with a clean message."""


def _run(command: list[str], *, capture: bool) -> str:
  """run a command with stderr passing through; return stripped stdout when capture."""
  stdout = subprocess.PIPE if capture else None
  result = spawn.run(command, stdout=stdout, text=True)
  if result.returncode != 0:
    raise LandError(f'`{" ".join(command[:3])}` failed with exit {result.returncode}')
  return result.stdout.strip() if capture else ''


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
  if decision == 'REVIEW_REQUIRED':
    return (
      f'PR #{pr["number"]} has no approving review and its base branch requires one '
      "(reviewDecision=REVIEW_REQUIRED); --no-review waives this command's check, not the "
      'branch rule GitHub enforces at the merge'
    )
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


def _head_error(pr: dict[str, Any]) -> Optional[str]:
  """the worktree holding something other than the reviewed head — an unpushed
  commit, or a branch left behind by whoever pushed the one under review."""
  head = git_out('rev-parse', 'HEAD')
  if head == pr['headRefOid']:
    return None
  return (
    f'PR #{pr["number"]} carries {pr["headRefOid"][:9]} and the worktree is on {head[:9]}; '
    'what lands is the reviewed head — push what you meant to land, or check it out'
  )


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
  'commits',
  'statusCheckRollup',
]


def _merge(pr: dict[str, Any]) -> None:
  """let GitHub replay the branch's commits onto the base, as they stand."""
  allowed = _run(['gh', 'api', 'repos/{owner}/{repo}', '--jq', '.allow_rebase_merge'], capture=True)
  if allowed != 'true':
    raise LandError(
      'the repository disallows rebase merging, so it cannot land the branch as it stands'
    )
  _run(
    [
      'gh',
      'pr',
      'merge',
      str(pr['number']),
      '--rebase',
      '--match-head-commit',
      pr['headRefOid'],
    ],
    capture=False,
  )


def _land(
  no_review: bool,
  allow_unchecked: bool,
  ignore_checks: bool,
  wait_checks: int,
) -> dict:
  pr = _pr_view(_PR_FIELDS)
  error = _precondition_error(pr, no_review, allow_unchecked) or _head_error(pr)
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
  _merge(pr)
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
    'commits': len(pr['commits']),
    'branch_deleted': _delete_remote_branch(pr['headRefName']),
  }


def land_pr(
  no_review: bool,
  allow_unchecked: bool,
  ignore_checks: bool,
  wait_checks: int,
) -> Optional[int]:
  try:
    result = _land(no_review, allow_unchecked, ignore_checks, wait_checks)
  except LandError as error:
    log.error(str(error))
    return 1
  print(json.dumps(result))
  return None


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='merge the approved PR for the current branch in one shot')
  parser.add_argument(
    '--no-review',
    action='store_true',
    help=(
      'merge without an APPROVED review (explicit user waiver; a review the base branch '
      'requires, or changes requested, still refuses)'
    ),
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
