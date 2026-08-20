import os
import subprocess
from pathlib import Path
from typing import Optional

from bro.base import log


def ensure_host_worktree(
  repository: Path, worktree: Path, branch: str, base_ref: Optional[str] = None
) -> bool:
  # create the worktree if new (git ops run in the project root, the cwd): a
  # `worktree-<name>` branch — based on base_ref (`--into`) when given, else on
  # the checkout's current HEAD — plus submodule alternates so `git submodule
  # update` reuses the superproject's modules, then a submodule init so the tree
  # is complete even when provisioning is skipped. an already-existing branch
  # defines its own base, so none of it applies there.
  if worktree.is_dir():
    return True
  log.info('creating worktree %s', worktree)
  branch_exists = (
    subprocess.run(
      ['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'],
      cwd=repository,
      capture_output=True,
    ).returncode
    == 0
  )
  quiet = [] if log.verbose_enabled() else ['-q']
  if branch_exists:
    add = ['git', 'worktree', 'add', *quiet, str(worktree), branch]
  else:
    base = base_ref if base_ref is not None else 'HEAD'
    add = ['git', 'worktree', 'add', *quiet, str(worktree), '-b', branch, base]
  if subprocess.run(add, cwd=repository).returncode != 0:
    log.error('failed to create worktree %s', worktree)
    return False
  for key, value in (
    ('submodule.alternateLocation', 'superproject'),
    ('submodule.alternateErrorStrategy', 'info'),
  ):
    subprocess.run(['git', '-C', str(worktree), 'config', key, value], check=False)
  if (
    subprocess.run(['git', '-C', str(worktree), 'submodule', 'update', '--init', *quiet]).returncode
    != 0
  ):
    log.error('failed to initialize submodules in %s', worktree)
    return False
  return True


def provision_host_worktree(worktree: Path) -> bool:
  # RIDE_VENV_MANIFEST is stripped: the container entrypoint exports it for the
  # baked /workspace venv, but a host worktree needs its own environment synced.
  script = worktree / 'setup.sh'
  if not script.is_file():
    log.info('%s not found; skipping project provisioning', script)
    return True
  env = {k: v for k, v in os.environ.items() if k != 'RIDE_VENV_MANIFEST'}
  if subprocess.run([str(script)], cwd=str(worktree), env=env).returncode != 0:
    log.error('failed to provision worktree %s', worktree)
    return False
  return True
