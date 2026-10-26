"""a job supervisor that withholds its ready signal until the test releases it."""

import sys
import textwrap
import time
from pathlib import Path

import pytest

from bro.base import spawn

_SUPERVISOR = 'bro.job_supervisor'
_HANG_SECONDS = 10.0


class StalledSupervisor:
  """stands in for the supervisor of every job started while installed: each one
  reports itself stalled and waits for `release()` before handing over to the
  real supervisor. A stall nobody releases expires after a hang bound and fails
  its start, so a test blocked behind it fails instead of hanging."""

  def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    self._stalled = directory / 'supervisor-stalled'
    self._released = directory / 'supervisor-released'
    self._expired = directory / 'supervisor-expired'
    script = directory / 'stalled_supervisor.py'
    script.write_text(
      textwrap.dedent(
        f"""\
        import os
        import sys
        import time
        from pathlib import Path

        Path({str(self._stalled)!r}).touch()
        deadline = time.monotonic() + {_HANG_SECONDS!r}
        while not Path({str(self._released)!r}).exists():
          if time.monotonic() > deadline:
            Path({str(self._expired)!r}).touch()
            sys.exit('no one released the stalled supervisor')
          time.sleep(0.01)
        supervisor = {spawn.module_argv(_SUPERVISOR)!r}
        os.execv(supervisor[0], [*supervisor, *sys.argv[1:]])
        """
      )
    )
    module_argv = spawn.module_argv
    monkeypatch.setattr(
      spawn,
      'module_argv',
      lambda module: (
        [sys.executable, str(script)] if module == _SUPERVISOR else module_argv(module)
      ),
    )

  def wait_stalled(self) -> None:
    deadline = time.monotonic() + _HANG_SECONDS
    while not self._stalled.exists():
      assert time.monotonic() < deadline, 'no job supervisor started'
      time.sleep(0.01)

  def expired(self) -> bool:
    return self._expired.exists()

  def release(self) -> None:
    self._released.touch()
