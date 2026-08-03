import os
import secrets
from pathlib import Path

from workspace.git import git_out


def venv_env(venv: Path) -> dict[str, str]:
  env = {**os.environ, 'VIRTUAL_ENV': str(venv)}
  env['PATH'] = str(venv / 'bin') + ':' + env.get('PATH', '')
  env.pop('PYTHONHOME', None)
  return env


def project_root() -> Path:
  """the repo whose workspaces, broker and summon state the caller shares.

  resolved through the shared git dir, so every linked worktree of a repo maps to
  its main checkout — one workspace namespace per repo. callers that mean the tree
  their own sources sit in want `base.source_root` instead.
  """
  return Path(git_out('rev-parse', '--git-common-dir')).resolve().parent


def worktrees_dir(project: Path) -> Path:
  return project / 'var' / 'cw' / 'worktrees'


def containers_dir(project: Path) -> Path:
  return project / 'var' / 'cw' / 'containers'


def fresh_workspace_name(base: str) -> str:
  """mint a workspace name absent from both local workspace namespaces."""
  project = project_root()
  worktrees = worktrees_dir(project)
  containers = containers_dir(project)
  while True:
    name = f'{base}-{secrets.token_hex(4)}'
    if not (worktrees / name).exists() and not (containers / name).exists():
      return name


def broker_dir(project: Path) -> Path:
  # the broker's control dir (one socket file per peer). deliberately shallow: the
  # host bind path must fit sun_path (~108 bytes), and a workspace-relative path
  # would land inside every container's /workspace mount.
  return project / 'var' / 'cw' / 'broker'


def summon_dir(project: Path) -> Path:
  # per-session summon audit and live-status files. outside the workspace dirs on
  # purpose: the audit must survive a workspace drop.
  return project / 'var' / 'cw' / 'summon'


def session_end_dir(project: Path) -> Path:
  # per-workspace record of how the last session ended (workspace/model.py:
  # record_session_end). outside the workspace dirs so the file never lands
  # inside a /workspace mount; removed with the workspace.
  return project / 'var' / 'cw' / 'exit'


def host_log_dir(project: Path) -> Path:
  # per-session host logs: where the outer launch process's mid-session output goes
  # while an interactive root owns the terminal (see workspace/spawn.py). outside
  # the workspace dirs so the file never lands inside a /workspace mount.
  return project / 'var' / 'cw' / 'log'


def in_container() -> bool:
  """detect a container by /.dockerenv presence. extracted so tests can stub it."""
  return Path('/.dockerenv').is_file()
