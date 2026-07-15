import os
import secrets
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


def _worktrees_dir(project: Path) -> Path:
  return project / 'var' / 'cw' / 'worktrees'


def _containers_dir(project: Path) -> Path:
  return project / 'var' / 'cw' / 'containers'


def fresh_workspace_name(base: str) -> str:
  """mint a workspace name absent from both local workspace namespaces."""
  project = _project_root()
  worktrees = _worktrees_dir(project)
  containers = _containers_dir(project)
  while True:
    name = f'{base}-{secrets.token_hex(4)}'
    if not (worktrees / name).exists() and not (containers / name).exists():
      return name


def _broker_dir(project: Path) -> Path:
  # the broker's control dir (one socket file per peer). deliberately shallow: the
  # host bind path must fit sun_path (~108 bytes), and a workspace-relative path
  # would land inside every container's /workspace mount.
  return project / 'var' / 'cw' / 'broker'


def _summon_dir(project: Path) -> Path:
  # per-session summon audit and live-status files. outside the workspace dirs on
  # purpose: the audit must survive a workspace drop.
  return project / 'var' / 'cw' / 'summon'


def _host_log_dir(project: Path) -> Path:
  # per-session host logs: where the outer cw process's mid-session output goes
  # while an interactive root owns the terminal (see cw/spawn.py). outside the
  # workspace dirs so the file never lands inside a /workspace mount.
  return project / 'var' / 'cw' / 'log'


def _session_claude_dir(name: str) -> Path:
  """the per-session claude state dir on the host — mounted as a container
  session's ~/.claude overlay."""
  return Path.home() / '.claude' / 'cw-sessions' / name


def _in_container() -> bool:
  """detect a container by /.dockerenv presence. extracted so tests can stub it."""
  return Path('/.dockerenv').is_file()


def _encode_claude_path(path: Path) -> str:
  """claude code's project-dir encoding of an absolute path: '/' and '.'
  replaced by '-'."""
  return str(path).replace('/', '-').replace('.', '-')


def _claude_config_dir() -> Path:
  """the claude config root of the current process's session: CLAUDE_CONFIG_DIR
  when set (a host session points it at its private per-session state dir), else
  the default ~/.claude (a container session's, via the cw-sessions mount)."""
  override = os.environ.get('CLAUDE_CONFIG_DIR')
  if override is not None:
    return Path(override)
  return Path.home() / '.claude'


def _claude_projects_dir(workspace: Path) -> Path:
  """claude code's per-project state dir for a workspace, under the session's
  config root. one derivation covers both modes — host worktree →
  `<encoded-worktree-path>`, container clone (`/workspace`) → `-workspace`."""
  return _claude_config_dir() / 'projects' / _encode_claude_path(workspace)


def _latest_jsonl(projects_dir: Path) -> Optional[Path]:
  if not projects_dir.is_dir():
    return None
  jsonls = [p for p in projects_dir.iterdir() if p.suffix == '.jsonl']
  if len(jsonls) == 0:
    return None
  return max(jsonls, key=lambda p: p.stat().st_mtime)
