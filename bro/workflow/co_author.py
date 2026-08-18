"""the commit trailer crediting the human an agent session works for.

A managed session commits as its bro, so the human who launched it appears
nowhere in the commit's authorship. The trailer is the credit GitHub keeps: its
merge rewrites the committer and preserves only the author and the message, and
it counts a co-author's commits toward their contributions as it counts an
author's.

The human is the workspace's own `user.name` / `user.email` — the bro identity
rides the `GIT_AUTHOR_*` environment and leaves the checkout's configured
identity as the launching human's.
"""

import re
from typing import Optional

from bro.launch.hold import interactive_session
from bro.llm import usage
from bro.workspace.git import git_run

TRAILER_KEY = 'Co-Authored-By'

_TRAILER_RE = re.compile(rf'^{TRAILER_KEY}:\s*.+$', re.MULTILINE | re.IGNORECASE)
# `git config --get` answers a key it does not hold with exit 1; every other
# nonzero code is git failing, which the commit that called it should not survive
_KEY_ABSENT = 1


def _git_config(key: str) -> str:
  """the checkout's effective value for `key`, empty when it declares none."""
  result = git_run('config', '--get', key)
  if result.returncode not in (0, _KEY_ABSENT):
    raise RuntimeError(f'git config --get {key} failed: {result.stderr.strip()}')
  return result.stdout.strip()


def trailer() -> Optional[str]:
  """the trailer line this session's commits carry, None when it credits nobody."""
  if not usage.agent_session() or not interactive_session():
    return None
  name, email = _git_config('user.name'), _git_config('user.email')
  if name == '' or email == '':
    return None
  return f'{TRAILER_KEY}: {name} <{email}>'


def strip_trailer(commit_message: str) -> str:
  """the message without its co-author trailer(s)."""
  return _TRAILER_RE.sub('', commit_message).rstrip()


def append_trailer(commit_message: str, line: str) -> str:
  """`commit_message` with `line` as its final paragraph.

  GitHub reads co-authorship from the last paragraph alone, so a trailer placed
  above anything else the message ends with credits nobody.
  """
  return f'{strip_trailer(commit_message).rstrip()}\n\n{line}\n'
