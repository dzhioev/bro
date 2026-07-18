#!/usr/bin/env python
"""squash-merge the approved PR for the current branch in one shot.

Runs the deterministic tail of a dev session: resolve the PR, enforce the merge
preconditions, inject the aggregated token footer into the squash body (see
setup/claude_commit_footer.py for the accounting model), merge, and delete the
remote feature branch.

Preconditions (each failure aborts with a message on stderr and exit 1):
- the PR for the current branch exists and is OPEN
- reviewDecision is APPROVED; `--no-review` waives a *missing* review, but
  CHANGES_REQUESTED is always refused
- the body has no unchecked `- [ ]` boxes unless `--allow-unchecked`

On success prints a single JSON object to stdout:

  {"pr": 310, "url": ..., "title": ..., "base": "master", "squash_sha": ...,
   "merged_at": "2026-07-03T10:41:02Z", "merged_at_minutes": "2026-07-03 10:41",
   "branch_deleted": true}

`merged_at_minutes` is UTC at minute precision, the format flow @-mentions parse.
"""

import json
import logging
import os
import re
import subprocess
from typing import Any, Optional

from base import spawn
from base.args import Parser

__cli_name__ = 'land-pr'

_log = logging.getLogger(__name__)


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


def _squash_footer(base: str) -> str:
  root = _run(['git', 'rev-parse', '--show-toplevel'], capture=True)
  # the repo's own copy, or the one vendored via the ppp submodule
  candidates = [
    f'{root}/setup/claude_commit_footer.py',
    f'{root}/ppp/setup/claude_commit_footer.py',
  ]
  footer_scripts = [path for path in candidates if os.path.isfile(path)]
  if len(footer_scripts) == 0:
    raise LandError(f'claude_commit_footer.py not found at any of: {", ".join(candidates)}')
  return _run([footer_scripts[0], '--squash', f'origin/{base}..HEAD'], capture=True)


def _body_with_footer(body: str, footer: str) -> str:
  trimmed = body.rstrip()
  if trimmed == '':
    return footer
  return f'{trimmed}\n\n{footer}'


def _merged_minutes(merged_at: str) -> str:
  return merged_at.replace('T', ' ')[:16]


def _delete_remote_branch(branch: str) -> bool:
  """delete only the remote ref — the local branch and checkout stay untouched
  (deleting them out from under a live worktree is the session manager's call).
  The merge is already done when this runs, so a failure degrades to a warning."""
  result = spawn.run(['git', 'push', 'origin', '--delete', branch], text=True)
  if result.returncode != 0:
    _log.warning(f'could not delete remote branch {branch} (exit {result.returncode})')
    return False
  return True


def _land(no_review: bool, allow_unchecked: bool) -> dict[str, Any]:
  pr = _pr_view(
    ['number', 'title', 'body', 'state', 'reviewDecision', 'baseRefName', 'headRefName', 'url']
  )
  error = _precondition_error(pr, no_review, allow_unchecked)
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
    'merged_at_minutes': _merged_minutes(merged['mergedAt']),
    'branch_deleted': _delete_remote_branch(pr['headRefName']),
  }


def land_pr(no_review: bool, allow_unchecked: bool) -> Optional[int]:
  try:
    result = _land(no_review, allow_unchecked)
  except LandError as error:
    _log.error(str(error))
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
  return land_pr(**parser.parse(argv))
