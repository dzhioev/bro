#!/usr/bin/env python
"""print the name of the branch origin's HEAD points at.

The remote itself answers, never `refs/remotes/origin/HEAD`: `git clone` writes
that ref once and nothing refreshes it when the remote's default changes.
"""

import re
from typing import Optional

from bro.base import log
from bro.base.args import Parser
from bro.workspace.git import git_run, no_prompt_env

__cli_name__ = 'default-branch'

_HEAD_SYMREF = re.compile(r'ref: refs/heads/(?P<branch>\S+)\tHEAD')


class DefaultBranchError(Exception):
  """origin is unreachable or names no branch as its HEAD."""


def read() -> str:
  """the default branch of the current repository's origin."""
  listing = git_run('ls-remote', '--symref', 'origin', 'HEAD', env=no_prompt_env())
  if listing.returncode != 0:
    raise DefaultBranchError(f'`git ls-remote origin` failed: {listing.stderr.strip()}')
  for line in listing.stdout.splitlines():
    match = _HEAD_SYMREF.fullmatch(line)
    if match is not None:
      return match['branch']
  raise DefaultBranchError('origin reports no branch as its HEAD')


def main(argv: list[str]) -> Optional[int]:
  Parser(description="print the name of origin's default branch").parse(argv)
  try:
    print(read())
  except DefaultBranchError as error:
    log.error(str(error))
    return 1
  return None
