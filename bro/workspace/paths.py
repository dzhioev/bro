import hashlib
import os
import secrets
from pathlib import Path
from typing import Optional

from bro.workspace.git import git_run

RUNTIME_BASE = Path('/var/ride')
CONTAINER_TRAILS_ROOT = Path('/var/ride/trails')
CONTAINER_SUMMON_ROOT = Path('/var/ride/summon')
_PROJECT_KEY_BYTES = 8


def venv_env(venv: Path) -> dict[str, str]:
  env = {**os.environ, 'VIRTUAL_ENV': str(venv)}
  env['PATH'] = str(venv / 'bin') + ':' + env.get('PATH', '')
  env.pop('PYTHONHOME', None)
  return env


def find_project_root(directory: Optional[Path] = None) -> Optional[Path]:
  """the repo whose runtime state `directory` shares, or None where nothing names
  one — no git on PATH, or no repository around it. The working directory is the
  default.

  resolved through the shared git dir, so every linked worktree of a repo maps to
  its main checkout. Separate checkouts retain separate roots.
  """
  try:
    if directory is None:
      result = git_run('rev-parse', '--git-common-dir')
    else:
      result = git_run('rev-parse', '--git-common-dir', cwd=directory)
  except FileNotFoundError:
    return None
  if result.returncode != 0:
    return None
  common_directory = Path(result.stdout.strip())
  if not common_directory.is_absolute():
    common_directory = (Path.cwd() if directory is None else directory) / common_directory
  return common_directory.resolve().parent


def project_root(directory: Optional[Path] = None) -> Path:
  """`find_project_root` for the callers a project is a precondition of."""
  root = find_project_root(directory)
  if root is None:
    subject = Path.cwd() if directory is None else directory
    raise ValueError(f'{subject} is in no git repository, so it names no project')
  return root


def project_key(project: Path) -> str:
  """the stable, path-derived key separating one checkout's runtime state."""
  canonical = str(project.resolve()).encode()
  return hashlib.blake2b(canonical, digest_size=_PROJECT_KEY_BYTES).hexdigest()


def runtime_root(project: Path) -> Path:
  return RUNTIME_BASE / project_key(project)


def require_runtime_root(project: Path) -> Path:
  """the setup-provisioned runtime root, or a launch-ready error."""
  root = runtime_root(project)
  if not root.is_dir():
    raise RuntimeError(
      f'runtime state root {root} is absent; run {project / "setup.sh"} on the host to create it'
    )
  if root.stat().st_uid != os.getuid() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
    raise RuntimeError(
      f'runtime state root {root} is not owned and writable by this user; '
      f'rerun {project / "setup.sh"} on the host'
    )
  return root


def workspaces_dir(project: Path) -> Path:
  return runtime_root(project) / 'workspaces'


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
  # One socket file lives here per peer. Keeping the root shallow leaves ample
  # room under the unix sun_path limit for the channel id.
  return runtime_root(project) / 'broker'


def trails_dir(project: Path) -> Path:
  if os.environ.get('RIDE_IN_CONTAINER') is not None:
    return CONTAINER_TRAILS_ROOT
  return runtime_root(project) / 'trails'


def summon_dir(project: Path) -> Path:
  # per-session summon audit and live-status files. outside the workspace dirs on
  # purpose: the audit must survive a workspace drop.
  return runtime_root(project) / 'summon'


def in_container() -> bool:
  """detect a container by /.dockerenv presence. extracted so tests can stub it."""
  return Path('/.dockerenv').is_file()
