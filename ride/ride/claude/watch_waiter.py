"""Claude `Stop` and `StopFailure` hook waking the session with its watches' lines.

Claude Code runs it with `asyncRewake`, in the background:
its exit 2 hands its stderr to the model, inside a running turn after the turn's next tool result, or as a turn of its own.
Every turn end starts one while the earlier ones keep running, so each registers as the session's current waiter, and one a later waiter supersedes exits 0.
Claude's `timeout`, the waiter's one argument, ends it with a SIGTERM that wakes nothing, so the waiter wakes the session a minute short of it instead.
"""

import os
import sys
import time
from typing import TextIO

from bro import watches
from ride.claude.waiter_state import REWAKE_STATUS, WaiterState

POLL_SECONDS = 0.5
_BOUND_MARGIN_SECONDS = 60


def _alive(process_id: int) -> bool:
  try:
    os.kill(process_id, 0)
  except ProcessLookupError:
    return False
  return True


def _bound_notice(bound_seconds: float) -> str:
  return f'No watch line arrived for {bound_seconds / 3600:g} hours; ending the turn keeps waiting.'


def wait(
  state: WaiterState,
  store: watches.Store,
  runner_process_id: int,
  bound_seconds: float,
  out: TextIO,
) -> int:
  """Write the store's next batch to `out` and return the rewake status, or
  return 0 once superseded, stood down, or outlived by the session runner."""
  with state.locked():
    token = state.register()
  deadline = time.monotonic() + bound_seconds - _BOUND_MARGIN_SECONDS
  while True:
    with state.locked():
      if state.stood_down() or not state.is_current(token) or not _alive(runner_process_id):
        return 0
      batch = store.take()
      if batch is None and time.monotonic() >= deadline:
        batch = _bound_notice(bound_seconds)
      if batch is not None:
        state.count_rewake()
        out.write(f'\n{batch}\n')
        out.flush()
        return REWAKE_STATUS
    time.sleep(POLL_SECONDS)


def main(argv: list[str]) -> int:
  bound_seconds = float(argv[1])
  # the hook input carries nothing the waiter needs; reading it lets Claude's
  # write complete however early the waiter exits
  sys.stdin.read()
  return wait(
    WaiterState.for_session(),
    watches.session_store(),
    int(os.environ['RIDE_RUNNER_PID']),
    bound_seconds,
    sys.stderr,
  )


if __name__ == '__main__':
  sys.exit(main(sys.argv))
