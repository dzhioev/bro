"""Resolve an explicit remote or local landing branch."""

import subprocess
from dataclasses import dataclass
from typing import Optional

from bro.base import spawn
from bro.workflow import default_branch
from bro.workspace.git import no_prompt_env


class WorkflowError(Exception):
  """A delivery precondition or command failed."""


def command(*arguments: str) -> str:
  result = spawn.run(arguments, capture_output=True, text=True, env=no_prompt_env())
  if result.returncode != 0:
    raise WorkflowError(output(result))
  return result.stdout.strip()


def output(result: subprocess.CompletedProcess[str]) -> str:
  return '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip())


def git(*arguments: str) -> str:
  return command('git', *arguments)


@dataclass(frozen=True)
class Base:
  ref: str
  branch: str
  remote: Optional[str]
  tip: str

  @property
  def destination(self) -> str:
    return self.remote if self.remote is not None else '.'


def _refusal(name: str, remotes: list[str]) -> WorkflowError:
  choices = []
  for remote in remotes:
    if git('ls-remote', '--heads', remote, f'refs/heads/{name}'):
      choices.append(f'{remote}/{name}')
  choices.append(f'refs/heads/{name}')
  return WorkflowError(
    f'base {name!r} is missing or not an explicit branch ref; use {", ".join(choices)}'
  )


def resolve(base: Optional[str]) -> Base:
  remotes = git('remote').splitlines()
  if base is None:
    if 'origin' not in remotes:
      raise WorkflowError('an omitted base needs origin; name a remote/branch or refs/heads/branch')
    base = f'origin/{default_branch.read()}'
  first, separator, rest = base.partition('/')
  if not base.startswith('refs/heads/') and first in remotes and separator:
    remote, branch = first, rest
    ref = f'refs/remotes/{remote}/{branch}'
    git('check-ref-format', f'refs/heads/{branch}')
    if not git('ls-remote', '--heads', remote, f'refs/heads/{branch}'):
      raise _refusal(branch, remotes)
    git('fetch', '--no-tags', remote, f'+refs/heads/{branch}:{ref}')
  else:
    remote = None
    if base.startswith('refs/heads/'):
      branch = base.removeprefix('refs/heads/')
    elif not remotes:
      branch = base
    else:
      raise _refusal(base, remotes)
    ref = f'refs/heads/{branch}'
    git('check-ref-format', ref)
  result = spawn.run(
    ['git', 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}'],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    raise _refusal(branch, remotes)
  return Base(ref=base, branch=branch, remote=remote, tip=result.stdout.strip())
