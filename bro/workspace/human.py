"""the human a managed session works for: their git identity, as the session
environment carries it.

A session commits as an identity of its own, so the human who launched it is
reachable only through what the launch put in the environment — read from the
repository it attached to, where their own configured identity lives.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.workspace.git import git_run

HUMAN_NAME_ENV = 'BRO_HUMAN_NAME'
HUMAN_EMAIL_ENV = 'BRO_HUMAN_EMAIL'

# `git config --get` answers a key it does not hold with exit 1; every other
# nonzero code is git failing, which the caller should not survive
_KEY_ABSENT = 1


@dataclass(frozen=True)
class Human:
  name: str
  email: str


def _git_config(repository: Path, key: str) -> str:
  """`repository`'s effective value for `key`, empty when it declares none."""
  result = git_run('config', '--get', key, cwd=repository)
  if result.returncode not in (0, _KEY_ABSENT):
    raise RuntimeError(f'git config --get {key} failed in {repository}: {result.stderr.strip()}')
  return result.stdout.strip()


def configured_human(repository: Path) -> Optional[Human]:
  """the identity `repository` resolves for `user.name` / `user.email`, None when
  either half is missing."""
  name = _git_config(repository, 'user.name')
  email = _git_config(repository, 'user.email')
  if name == '' or email == '':
    return None
  return Human(name, email)


def human_env(human: Human) -> dict[str, str]:
  """the environment carrying `human` into a session."""
  return {HUMAN_NAME_ENV: human.name, HUMAN_EMAIL_ENV: human.email}


def session_human() -> Optional[Human]:
  """the human this session's launch named, None when it named none."""
  name, email = os.environ.get(HUMAN_NAME_ENV), os.environ.get(HUMAN_EMAIL_ENV)
  if name is None and email is None:
    return None
  if name is None or email is None:
    raise RuntimeError(
      f'half a human identity in the environment: {HUMAN_NAME_ENV}='
      f'{"unset" if name is None else name!r}, {HUMAN_EMAIL_ENV}='
      f'{"unset" if email is None else email!r}'
    )
  return Human(name, email)
