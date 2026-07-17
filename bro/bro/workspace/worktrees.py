import os
import subprocess
from pathlib import Path
from typing import Optional

from base import log


def _ensure_host_worktree(worktree: Path, branch: str, base_ref: Optional[str] = None) -> bool:
  # create the worktree if new (git ops run in the project root, the cwd): a
  # `worktree-<name>` branch — based on base_ref (`--into`) when given, else on
  # the checkout's current HEAD — plus submodule alternates so `git submodule
  # update` reuses the superproject's modules. an already-existing branch defines
  # its own base, so neither applies there.
  if worktree.is_dir():
    return True
  log.info('creating worktree %s', worktree)
  branch_exists = (
    subprocess.run(
      ['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], capture_output=True
    ).returncode
    == 0
  )
  quiet = [] if log.verbose_enabled() else ['-q']
  if branch_exists:
    add = ['git', 'worktree', 'add', *quiet, str(worktree), branch]
  else:
    base = base_ref if base_ref is not None else 'HEAD'
    add = ['git', 'worktree', 'add', *quiet, str(worktree), '-b', branch, base]
  if subprocess.run(add).returncode != 0:
    log.error('failed to create worktree %s', worktree)
    return False
  for key, value in (
    ('submodule.alternateLocation', 'superproject'),
    ('submodule.alternateErrorStrategy', 'info'),
  ):
    subprocess.run(['git', '-C', str(worktree), 'config', key, value], check=False)
  return True


def _provision_host_worktree(worktree: Path) -> bool:
  # run the worktree's own provision_repo.sh against itself (idempotent: skips the
  # uv sync when the venv is current, always refreshes the console-script bridge +
  # git hooks). shared with host setup_repo.sh and the container entrypoint.
  # CW_VENV_BAKED is stripped: the container entrypoint exports it for the baked
  # /workspace venv, but a worktree venv (the in-container nesting fallback) is
  # freshly synced and still needs its console-script bridge generated.
  script = worktree / 'setup' / 'provision_repo.sh'
  if not script.is_file():
    log.warning('%s not found (worktree on an old base?); skipping provisioning', script)
    return True
  env = {k: v for k, v in os.environ.items() if k != 'CW_VENV_BAKED'}
  if subprocess.run([str(script)], cwd=str(worktree), env=env).returncode != 0:
    log.error('failed to provision worktree %s', worktree)
    return False
  return True
