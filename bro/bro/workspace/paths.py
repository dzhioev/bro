import os
from pathlib import Path
from typing import Optional

from cw.git import git_out


def _venv_env(venv: Path) -> dict[str, str]:
  env = {**os.environ, 'VIRTUAL_ENV': str(venv)}
  env['PATH'] = str(venv / 'bin') + ':' + env.get('PATH', '')
  env.pop('PYTHONHOME', None)
  return env


def _project_root() -> Path:
  return Path(git_out('rev-parse', '--git-common-dir')).resolve().parent


def _worktrees_dir(proj: Path) -> Path:
  return proj / 'var' / 'cw' / 'worktrees'


def _containers_dir(proj: Path) -> Path:
  return proj / 'var' / 'cw' / 'containers'


def _broker_dir(proj: Path) -> Path:
  # the broker's control dir (one socket file per peer). deliberately shallow: the
  # host bind path must fit sun_path (~108 bytes), and a workspace-relative path
  # would land inside every container's /workspace mount.
  return proj / 'var' / 'cw' / 'broker'


def _in_container() -> bool:
  """detect a container by /.dockerenv presence. extracted so tests can stub it."""
  return Path('/.dockerenv').is_file()


def _latest_jsonl(projects_dir: Path) -> Optional[Path]:
  if not projects_dir.is_dir():
    return None
  jsonls = [p for p in projects_dir.iterdir() if p.suffix == '.jsonl']
  if len(jsonls) == 0:
    return None
  return max(jsonls, key=lambda p: p.stat().st_mtime)
