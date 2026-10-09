"""Session-local monitoring paths and signals shared across package boundaries."""

import os
from pathlib import Path, PurePath
from typing import Optional

SESSION_DIR_ENV = 'RIDE_SESSION_DIR'
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
