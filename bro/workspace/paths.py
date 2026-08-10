import os
import secrets
from pathlib import Path

from bro.workspace.git import git_out


def venv_env(venv: Path) -> dict[str, str]:
  env = {**os.environ, 'VIRTUAL_ENV': str(venv)}
  env['PATH'] = str(venv / 'bin') + ':' + env.get('PATH', '')
  env.pop('PYTHONHOME', None)
  return env


def project_root() -> Path:
  """the repo whose workspaces, broker and summon state the caller shares.

  resolved through the shared git dir, so every linked worktree of a repo maps to
  its main checkout — one workspace namespace per repo. callers that mean the tree
  their own sources sit in want `bro.base.source_root` instead.
  """
  return Path(git_out('rev-parse', '--git-common-dir')).resolve().parent


def workspaces_dir(project: Path) -> Path:
  return project / 'var' / 'cw' / 'workspaces'


def workspace_dir(project: Path, name: str) -> Path:
  """a workspace's own directory: its tree plus every record kept about it."""
  return workspaces_dir(project) / name


def workspace_tree(project: Path, name: str) -> Path:
  # a subdirectory rather than the workspace dir itself: a container binds the
  # tree as /workspace, and the workspace's records must stay outside that mount.
  return workspace_dir(project, name) / 'tree'


def fresh_workspace_name(base: str) -> str:
  """mint a workspace name that no local workspace holds."""
  project = project_root()
  while True:
    name = f'{base}-{secrets.token_hex(4)}'
    if not workspace_dir(project, name).exists():
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


def in_container() -> bool:
  """detect a container by /.dockerenv presence. extracted so tests can stub it."""
  return Path('/.dockerenv').is_file()
