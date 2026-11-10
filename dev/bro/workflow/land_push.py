#!/usr/bin/env python
"""fast-forward a base to HEAD, optionally requiring verdict coverage and GitHub CI.

Exit 2 means the base moved; other refusals exit 1. Success prints the base,
pushed commits, verdict coverage, check run and branch deletions as JSON.
"""

import json
import re
from typing import Any, Optional

from bro.base import log, spawn
from bro.base.args import Parser
from bro.workflow import base_ref, default_branch, head_checks
from bro.workspace.git import no_prompt_env

__cli_name__ = 'land-push'
BASE_MOVED = 2


class BaseMoved(base_ref.WorkflowError):
  """HEAD cannot fast-forward the base."""


def _coverage(approved: Optional[str], base: str, head: str) -> Optional[str]:
  if approved is None:
    return None
  approved = base_ref.git('rev-parse', '--verify', f'{approved}^{{commit}}')
  if approved == head:
    return 'head'
  start = base_ref.git('merge-base', approved, base)
  if base_ref.git('rev-list', '--merges', f'{start}..{approved}', f'{base}..{head}'):
    raise base_ref.WorkflowError(
      'the approved head does not cover HEAD: an unchanged rebase cannot contain merge commits'
    )
  difference = base_ref.git(
    'range-diff',
    '--no-color',
    '--no-dual-color',
    f'{start}..{approved}',
    f'{base}..{head}',
  )
  lines = difference.splitlines()
  if not lines or any(
    re.fullmatch(r'\s*\d+:\s+[0-9a-f]+ = \d+:\s+[0-9a-f]+ .*', line) is None for line in lines
  ):
    raise base_ref.WorkflowError(
      'the approved head does not cover HEAD: range-diff has changed commits'
    )
  return 'range-diff'


def _land(
  base: Optional[str],
  approved: Optional[str],
  workflow: Optional[str],
  wait: int,
  delete: list[str],
) -> dict[str, Any]:
  if base_ref.git('status', '--porcelain'):
    raise base_ref.WorkflowError(
      'the worktree is dirty; commit or remove its changes before landing'
    )
  if wait < 0:
    raise base_ref.WorkflowError('--wait must be non-negative')
  resolved = base_ref.resolve(base)
  if delete and resolved.remote is None:
    raise base_ref.WorkflowError('--delete requires a remote base')
  for branch in delete:
    if branch.startswith('refs/'):
      raise base_ref.WorkflowError('--delete takes a branch name, not a refs/ path')
    base_ref.git('check-ref-format', f'refs/heads/{branch}')
    if branch == resolved.branch:
      raise base_ref.WorkflowError('--delete must not name the landing base')
  head = base_ref.git('rev-parse', 'HEAD')
  ancestor = spawn.run(['git', 'merge-base', '--is-ancestor', resolved.tip, head])
  if ancestor.returncode == 1:
    raise BaseMoved('base moved: HEAD does not descend from the base tip')
  if ancestor.returncode != 0:
    raise base_ref.WorkflowError('could not compare HEAD with the base tip')
  coverage = _coverage(approved, resolved.tip, head)
  checks = None if workflow is None else head_checks.ensure(resolved, workflow, head, wait)
  if checks is not None and not head_checks.passed(checks):
    raise base_ref.WorkflowError(f'HEAD checks did not pass: {json.dumps(checks)}')
  commits = base_ref.git('rev-list', '--reverse', f'{resolved.tip}..{head}').splitlines()
  pushed = spawn.run(
    ['git', 'push', '--porcelain', resolved.destination, f'{head}:refs/heads/{resolved.branch}'],
    capture_output=True,
    text=True,
    env=no_prompt_env(),
  )
  if pushed.returncode != 0:
    if any(
      reason in pushed.stdout
      for reason in ('[rejected] (non-fast-forward)', '[rejected] (fetch first)')
    ):
      raise BaseMoved(f'base moved: {base_ref.output(pushed)}')
    raise base_ref.WorkflowError(base_ref.output(pushed))
  deleted = []
  for branch in delete:
    base_ref.git('push', resolved.destination, '--delete', f'refs/heads/{branch}')
    deleted.append(branch)
  return {
    'base': resolved.ref,
    'head': head,
    'commits': commits,
    'coverage': coverage,
    'checks': checks,
    'deleted': deleted,
  }


def land_push(
  base: Optional[str],
  approved: Optional[str],
  workflow: Optional[str],
  wait: int,
  delete: list[str],
) -> Optional[int]:
  try:
    result = _land(base, approved, workflow, wait, delete)
  except BaseMoved as error:
    log.error(str(error))
    return BASE_MOVED
  except (base_ref.WorkflowError, default_branch.DefaultBranchError) as error:
    log.error(str(error))
    return 1
  print(json.dumps(result))
  return None


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description=__doc__)
  parser.add_argument(
    '--base', metavar='REF', help="remote/branch or refs/heads/branch (default: origin's default)"
  )
  parser.add_argument('--approved', metavar='SHA', help='head the verdict approved')
  parser.add_argument(
    '--workflow', metavar='FILE', help='require green checks, dispatching this workflow if none ran'
  )
  parser.add_argument(
    '--wait',
    type=int,
    default=head_checks.DEFAULT_WAIT,
    metavar='SECONDS',
    help='maximum wait for CI',
  )
  parser.add_argument(
    '--delete',
    action='append',
    default=[],
    metavar='BRANCH',
    help='branch to delete on the base remote after landing (repeatable)',
  )
  return land_push(**parser.parse(argv))
