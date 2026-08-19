"""Session-local monitoring paths and signals shared across package boundaries."""

import os
from pathlib import Path
from typing import Optional

SESSION_DIR_ENV = 'RIDE_SESSION_DIR'
CLAUDE_CONFIG_DIR_ENV = 'CLAUDE_CONFIG_DIR'


def session_dir() -> Optional[Path]:
  """the session's own state directory, or None where the process runs outside a
  managed session and so keeps no session state."""
  value = os.environ.get(SESSION_DIR_ENV)
  return Path(value) if value is not None else None


def harness_session_dir(harness: str) -> Optional[Path]:
  """the session-state subdirectory holding one harness's own artifacts, beside
  the signals every harness shares."""
  session = session_dir()
  return session / harness if session is not None else None


def workspace_session_dir(workspace: Path) -> Path:
  """a managed workspace's session state dir — a workspace record like any
  other, host-side in both session modes."""
  return workspace / 'session'


def in_claude_session() -> bool:
  """whether this process runs inside a claude session, the only kind claude
  keeps config and transcripts for."""
  return os.environ.get(CLAUDE_CONFIG_DIR_ENV) is not None


def claude_config_dir() -> Path:
  """the session's claude config root."""
  override = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
  if override is None:
    raise RuntimeError(f'{CLAUDE_CONFIG_DIR_ENV} is unset: this is no claude session')
  return Path(override)


def workspace_claude_dir(workspace: Path) -> Path:
  """a managed workspace's claude config root — a workspace record like any
  other, host-side in both session modes (a container mounts it as its
  `~/.claude`)."""
  return workspace / 'claude'


def encode_project_path(path: Path) -> str:
  """claude code's project-dir encoding of an absolute path: '/' and '.'
  replaced by '-'."""
  return str(path).replace('/', '-').replace('.', '-')


def claude_projects_dir(workspace: Path) -> Path:
  """claude code's transcript dir for a workspace, under the active config root."""
  return claude_config_dir() / 'projects' / encode_project_path(workspace)


def working_projects_dir() -> Path:
  """the transcript dir claude keeps for the working directory's session: the
  nearest ancestor that already has one, else the working directory's own."""
  pwd = os.environ.get('PWD')
  cwd = Path(pwd if pwd is not None else os.getcwd()).resolve()
  for candidate in [cwd, *cwd.parents]:
    project_dir = claude_projects_dir(candidate)
    if project_dir.is_dir():
      return project_dir
  return claude_projects_dir(cwd)
