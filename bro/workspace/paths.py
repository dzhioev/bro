import os
import re
import secrets
from pathlib import Path
from typing import Optional

from bro.workspace.git import git_run

CONTAINER_TRAILS_ROOT = Path('/var/ride/trails')
CONTAINER_SUMMON_ROOT = Path('/var/ride/summon')
CONTAINER_SESSION_DIR = Path('/var/ride/session')
_DATA_HOME_ENV = 'XDG_DATA_HOME'
# a name is one path component, and also becomes a git branch and a docker
# container name; this is the narrowest of the three
_WORKSPACE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


class RuntimeLocationError(ValueError):
  """the environment names no usable runtime or repository location."""


class WorkspaceNameError(ValueError):
  """a workspace name is not usable as a directory name."""


def venv_env(venv: Path) -> dict[str, str]:
  env = {**os.environ, 'VIRTUAL_ENV': str(venv)}
  env['PATH'] = str(venv / 'bin') + ':' + env.get('PATH', '')
  env.pop('PYTHONHOME', None)
  return env


def find_project_root(directory: Optional[Path] = None) -> Optional[Path]:
  """the checkout root containing `directory`, or None when there is no checkout.

  Resolved through the shared git dir, so every linked worktree of a repository
  maps to its main checkout. Separate checkouts retain separate roots.
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
  """`find_project_root` for callers that require a repository."""
  root = find_project_root(directory)
  if root is None:
    subject = Path.cwd() if directory is None else directory
    raise RuntimeLocationError(f'{subject} is in no git repository')
  return root


def runtime_base() -> Path:
  """the user's runtime data root, `$XDG_DATA_HOME/ride` where that is set."""
  data_home = os.environ.get(_DATA_HOME_ENV)
  if data_home is None or len(data_home) == 0:
    return Path.home() / '.local' / 'share' / 'ride'
  base = Path(data_home)
  if not base.is_absolute():
    raise RuntimeLocationError(f'{_DATA_HOME_ENV} must be an absolute path, not {data_home!r}')
  return base / 'ride'


def ensure_runtime_root() -> Path:
  """create the global runtime root and keep it private."""
  root = runtime_base()
  root.mkdir(parents=True, exist_ok=True)
  root.chmod(0o700)
  return root


def workspaces_dir() -> Path:
  return runtime_base() / 'workspaces'


def is_workspace_name(name: str) -> bool:
  return _WORKSPACE_NAME.fullmatch(name) is not None


def workspace_dir(name: str) -> Path:
  """a workspace's own directory: its tree plus every record kept about it."""
  if not is_workspace_name(name):
    raise WorkspaceNameError(f'not a usable workspace name: {name!r}')
  return workspaces_dir() / name


def workspace_tree(name: str) -> Path:
  return workspace_dir(name) / 'tree'


def fresh_workspace_name(base: str) -> str:
  """mint a workspace name that no local workspace holds."""
  while True:
    name = f'{base}-{secrets.token_hex(4)}'
    if not workspace_dir(name).exists():
      return name


def broker_dir() -> Path:
  return runtime_base() / 'broker'


def trails_dir() -> Path:
  if os.environ.get('RIDE_IN_CONTAINER') is not None:
    return CONTAINER_TRAILS_ROOT
  return runtime_base() / 'trails'


def summon_dir() -> Path:
  return runtime_base() / 'summon'


def in_container() -> bool:
  """detect a container by /.dockerenv presence. extracted so tests can stub it."""
  return Path('/.dockerenv').is_file()
