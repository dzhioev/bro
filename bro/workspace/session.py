"""in-session control of a managed session.

A managed session is supervised by an in-place runner that exports
`RIDE_RUNNER_PID` (its own pid) next to the harness process; session-owned
processes end the session by signaling that pid.
"""

import os
import signal


def terminate_session() -> None:
  """end the running session from a session-owned process: SIGTERM the in-place
  runner (RIDE_RUNNER_PID), which ends the harness process it supervises and
  survives to run its teardown. raises when no runner pid is set."""
  os.kill(int(os.environ['RIDE_RUNNER_PID']), signal.SIGTERM)
