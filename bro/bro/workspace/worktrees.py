import subprocess
from pathlib import Path
from typing import Optional

from base import log
from base.yesno import yesno
from cw.workspace import HostWorktree


def _ensure_host_worktree(worktree: Path, branch: str, base_ref: Optional[str] = None) -> bool:
  # create the worktree if new (git ops run in the project root, the cwd): a
  # `worktree-<name>` branch — based on base_ref (`--into`) when given, else the
  # current HEAD — plus submodule alternates so `git submodule update` reuses the
  # superproject's modules. an already-existing branch defines its own base, so
  # base_ref doesn't apply there.
  if worktree.is_dir():
    return True
  log.info('creating worktree %s', worktree)
  branch_exists = (
    subprocess.run(
      ['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], capture_output=True
    ).returncode
    == 0
  )
  if branch_exists:
    add = ['git', 'worktree', 'add', str(worktree), branch]
  else:
    base = [base_ref] if base_ref is not None else []
    add = ['git', 'worktree', 'add', str(worktree), '-b', branch, *base]
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
  script = worktree / 'setup' / 'provision_repo.sh'
  if not script.is_file():
    log.warning('%s not found (worktree on an old base?); skipping provisioning', script)
    return True
  if subprocess.run([str(script)], cwd=str(worktree)).returncode != 0:
    log.error('failed to provision worktree %s', worktree)
    return False
  return True


def _finish_host_worktree(ws: HostWorktree, *, interactive: bool) -> None:
  # on exit, warn if the worktree isn't landed on origin/master, then (interactive
  # only) offer to drop it. non-interactive sessions keep it — safe default, and the
  # path stays correct if --auto/--bro ever run on host. `cw clean` removes it later.
  _, reasons = ws.is_clean()
  if len(reasons) > 0:
    log.warning('worktree %s not landed on origin/master:', ws.name)
    for reason in reasons:
      log.warning('  - %s', reason)
  if not interactive:
    return
  if yesno(f'drop worktree {ws.name}?'):
    ws.remove()
