import os
from pathlib import Path

from bro.native.run_log import RunLog


def session_environment(directory: Path) -> dict[str, str]:
  return {
    **os.environ,
    'BRO_STORE': str(directory / 'absent-credentials'),
    'RIDE_SESSION_DIR': str(directory / 'session'),
    'RIDE_WORKSPACE': 'test-session',
    'RIDE_HOST': 'test-host',
    'RIDE_HOST_WORKSPACE': str(directory / 'tree'),
    'RIDE_ISOLATION': 'unboxed',
    'RIDE_COMMAND': 'ride along test-session',
  }


def open_log(path: Path, *, interactive: bool = False) -> RunLog:
  writer = RunLog(path)
  writer.start('dev', {'type': 'echo', 'model': 'echo'}, 'system', None, interactive, 'test')
  return writer
