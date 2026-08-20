"""in-session control of a managed session.

A managed session is supervised by an in-place runner that exports
`RIDE_RUNNER_PID` (its own pid) next to the harness process; session-owned
processes end the session by signaling that pid, leaving in the session's state
dir the status the session is to report.
"""

import os
import signal
from pathlib import Path
from typing import Optional

from bro.monitor import SESSION_DIR_ENV, session_dir

FILENAME = 'exit-status'


def terminate_session(status: int) -> None:
  """end the running session from a session-owned process, `status` becoming the
  session's own exit status: SIGTERM the in-place runner (RIDE_RUNNER_PID),
  which ends the harness process it supervises and survives to run its
  teardown. raises outside a managed session."""
  session = session_dir()
  if session is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset: this is no managed session')
  session.mkdir(parents=True, exist_ok=True)
  (session / FILENAME).write_text(str(status))
  os.kill(int(os.environ['RIDE_RUNNER_PID']), signal.SIGTERM)


def requested_exit_status() -> Optional[int]:
  """the status a session-owned process asked this session to report, or None
  when none did."""
  requested = _requested_path()
  if requested is None or not requested.is_file():
    return None
  return int(requested.read_text())


def clear_requested_exit_status() -> None:
  """drop what an earlier run in this workspace asked for, so only this
  session's own request is read."""
  requested = _requested_path()
  if requested is not None:
    requested.unlink(missing_ok=True)


def _requested_path() -> Optional[Path]:
  session = session_dir()
  return session / FILENAME if session is not None else None
