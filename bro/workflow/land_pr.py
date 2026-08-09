#!/usr/bin/env python
"""squash-merge the approved PR for the current branch in one shot.

Runs the deterministic tail of a dev session: resolve the PR, enforce the merge
preconditions, append the branch's aggregated token footer when its commits
carry any (`commit-footer --squash`), merge, and delete the remote feature
branch.

Preconditions (each failure aborts with a message on stderr and exit 1):
- the PR for the current branch exists and is OPEN
- reviewDecision is APPROVED; `--no-review` waives a *missing* review, but
  CHANGES_REQUESTED is always refused
- the body has no unchecked `- [ ]` boxes unless `--allow-unchecked`
- every status check has concluded and passed. Pending checks are waited out
  (`--wait-checks` seconds) and then refuse the merge, as a failed check does.
  A PR with no checks passes. `--ignore-checks` drops the gate whole — no
  wait, no refusal, whatever the checks say.

On success prints a single JSON object to stdout:

  {"pr": 310, "url": ..., "title": ..., "base": "master", "squash_sha": ...,
   "merged_at": "2026-07-03T10:41:02Z", "branch_deleted": true}
"""

import json
import re
import subprocess
import time
from typing import Any, Optional

from bro.base import log, spawn
from bro.base.args import Parser
from bro.extra.github import api

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


def _squash_footer(base: str) -> str:
  # empty for a branch with no footered commits (commit-footer's own scoping)
  return _run(['commit-footer', '--squash', f'origin/{base}..HEAD'], capture=True)


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


def _land(no_review: bool, allow_unchecked: bool, ignore_checks: bool, wait_checks: int) -> dict:
  pr = _pr_view(
    [
      'number',
      'title',
      'body',
      'state',
      'reviewDecision',
      'baseRefName',
      'headRefName',
      'url',
      'statusCheckRollup',
    ]
  )
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
  footer = _squash_footer(pr['baseRefName'])
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
  merged = _pr_view(['state', 'mergeCommit', 'mergedAt'], number=pr['number'])
  if merged['state'] != 'MERGED':
    raise LandError(f'merge command succeeded but PR state is {merged["state"]}')
  return {
    'pr': pr['number'],
    'url': pr['url'],
    'title': pr['title'],
    'base': pr['baseRefName'],
    'squash_sha': merged['mergeCommit']['oid'],
    'merged_at': merged['mergedAt'],
    'branch_deleted': _delete_remote_branch(pr['headRefName']),
  }


def land_pr(
  no_review: bool, allow_unchecked: bool, ignore_checks: bool, wait_checks: int
) -> Optional[int]:
  try:
    result = _land(no_review, allow_unchecked, ignore_checks, wait_checks)
  except LandError as error:
    log.error(str(error))
    return 1
  print(json.dumps(result))
  return None


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='squash-merge the approved PR for the current branch in one shot')
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
