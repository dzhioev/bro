#!/usr/bin/env python
"""run GitHub CI on HEAD without landing it; print the head, run and conclusion as JSON."""

import json
from typing import Optional

from bro.base import log
from bro.base.args import Parser
from bro.workflow import base_ref, default_branch, head_checks

__cli_name__ = 'check-head'


def check_head(base: Optional[str], workflow: str, wait: int) -> Optional[int]:
  try:
    if wait < 0:
      raise base_ref.WorkflowError('--wait must be non-negative')
    resolved = base_ref.resolve(base)
    result = head_checks.dispatch(resolved, workflow, base_ref.git('rev-parse', 'HEAD'), wait)
  except (base_ref.WorkflowError, default_branch.DefaultBranchError) as error:
    log.error(str(error))
    return 1
  print(json.dumps(result))
  return None if head_checks.passed(result) else 1


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description=__doc__)
  parser.add_argument(
    '--base', metavar='REF', help="remote/branch (default: origin's default branch)"
  )
  parser.add_argument(
    '--workflow', required=True, metavar='FILE', help='workflow accepting workflow_dispatch'
  )
  parser.add_argument(
    '--wait',
    type=int,
    default=head_checks.DEFAULT_WAIT,
    metavar='SECONDS',
    help='maximum wait for CI',
  )
  return check_head(**parser.parse(argv))
