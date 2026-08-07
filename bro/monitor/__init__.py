"""Session-local monitoring paths and signals shared across package boundaries."""

import os
from pathlib import Path


def claude_config_dir() -> Path:
  """the active Claude config root, defaulting to the user-level directory."""
  override = os.environ.get('CLAUDE_CONFIG_DIR')
  return Path(override) if override is not None else Path.home() / '.claude'


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
