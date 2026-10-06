"""The session's watch waiter as its waiters and the runner share it:
which waiter is current, how many rewakes waiters have begun, and the runner's
stand-down mark."""

import contextlib
import fcntl
import os
import shutil
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Self

from bro.monitor import SESSION_DIR_ENV, harness_session_dir

# the hook events Claude Code runs the waiter on
WAITER_EVENTS = ('Stop', 'StopFailure')
# the exit status that wakes the model with an `asyncRewake` hook's stderr
REWAKE_STATUS = 2
# the first line of a waiter hook's stdout, which tells its hook events from
# those of the hooks Claude merges in from the session's other settings
WAITER_MARK = 'ride.claude.watch_waiter'

_CURRENT_FILENAME = 'current'
_STOOD_DOWN_FILENAME = 'stood-down'
_REWAKES_FILENAME = 'rewakes'
_LOCK_FILENAME = '.lock'


class WaiterState:
  def __init__(self, directory: Path):
    self.directory = directory

  @classmethod
  def for_session(cls) -> Self:
    state = harness_session_dir('claude')
    if state is None:
      raise RuntimeError(f'{SESSION_DIR_ENV} is unset: a watch waiter needs a session state dir')
    return cls(state / 'waiter')

  def reset(self) -> None:
    """Forget what an earlier session in this state dir left."""
    if self.directory.exists():
      shutil.rmtree(self.directory)
    self.directory.mkdir(parents=True)

  @contextlib.contextmanager
  def locked(self) -> Generator[None]:
    """Hold the lock under which a waiter takes a batch and the runner decides an end."""
    with (self.directory / _LOCK_FILENAME).open('a') as lock_file:
      fcntl.flock(lock_file, fcntl.LOCK_EX)
      yield

  def register(self) -> str:
    """Make the calling waiter the current one and return its token; called under `locked`."""
    token = uuid.uuid4().hex
    self._replace(_CURRENT_FILENAME, token)
    return token

  def registered(self) -> bool:
    """Whether a waiter has registered since the reset."""
    return (self.directory / _CURRENT_FILENAME).exists()

  def is_current(self, token: str) -> bool:
    return (self.directory / _CURRENT_FILENAME).read_text() == token

  def stand_down(self) -> None:
    (self.directory / _STOOD_DOWN_FILENAME).touch()

  def stood_down(self) -> bool:
    return (self.directory / _STOOD_DOWN_FILENAME).exists()

  def count_rewake(self) -> None:
    """Record a rewake a waiter begins; called under `locked`."""
    self._replace(_REWAKES_FILENAME, str(self.rewakes() + 1))

  def rewakes(self) -> int:
    try:
      raw = (self.directory / _REWAKES_FILENAME).read_text()
    except FileNotFoundError:
      return 0
    if not raw.isdigit():
      raise RuntimeError(f'{self.directory / _REWAKES_FILENAME} carries a malformed count')
    return int(raw)

  def _replace(self, filename: str, content: str) -> None:
    path = self.directory / filename
    staging = path.with_name(f'{filename}.{os.getpid()}.tmp')
    staging.write_text(content)
    os.replace(staging, path)
