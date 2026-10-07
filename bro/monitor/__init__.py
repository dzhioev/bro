"""Session-local monitoring paths and signals shared across package boundaries."""

import os
from pathlib import Path, PurePath
from typing import Optional

SESSION_DIR_ENV = 'RIDE_SESSION_DIR'
CLAUDE_CONFIG_DIR_ENV = 'CLAUDE_CONFIG_DIR'
# the session runner's pid/start-time record in its session dir, written at
# session start and removed at exit — what a supervisor kills a session by
PROCESS_FILENAME = 'runner.pid'


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


def workspace_session_dir[PathT: PurePath](workspace: PathT) -> PathT:
  """a managed workspace's session state dir — a workspace record like any
  other, host-side in both session modes. Also composes a records root's
  container-side spelling, which is why the path flavor is the caller's."""
  return workspace / 'session'


def workspace_party_dir(workspace: Path) -> Path:
  """The root under which a workspace keeps its joined sessions' records."""
  return workspace / 'party'


def party_member_dir(workspace: Path, member: str) -> Path:
  """The records root of one session that joined the workspace's party."""
  return workspace_party_dir(workspace) / member


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
  """claude code's project-dir encoding of an absolute path, over the UTF-16
  code units its JavaScript reads: every unit outside `[a-zA-Z0-9]` becomes
  '-', and a name longer than 200 units is cut there and suffixed with '-' and
  the base-36 magnitude of the path's 32-bit string hash."""
  data = str(path).encode('utf-16-le', 'surrogatepass')
  units = [int.from_bytes(data[index : index + 2], 'little') for index in range(0, len(data), 2)]
  name = ''.join(
    chr(unit) if chr(unit).isascii() and chr(unit).isalnum() else '-' for unit in units
  )
  if len(name) <= _PROJECT_NAME_LIMIT:
    return name
  return f'{name[:_PROJECT_NAME_LIMIT]}-{_base36(abs(_string_hash(units)))}'


_PROJECT_NAME_LIMIT = 200
_BASE36_DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'


def _string_hash(units: list[int]) -> int:
  """JavaScript's `(h << 5) - h + unit | 0` fold, as a signed 32-bit value."""
  value = 0
  for unit in units:
    value = (value * 31 + unit) & 0xFFFFFFFF
  return value - (1 << 32) if value >= (1 << 31) else value


def _base36(value: int) -> str:
  digits = ''
  while True:
    value, digit = divmod(value, 36)
    digits = _BASE36_DIGITS[digit] + digits
    if value == 0:
      return digits


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
