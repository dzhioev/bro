"""The session's watches: commands run once for the session whose lines are kept
under the session state dir, so a reader picks up where the previous one stopped.

`watch-run` declares a watch and appends the command's lines to its log;
`watch-next` delivers the lines past the offset it keeps per watch.
"""

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.monitor import SESSION_DIR_ENV, session_dir

WATCH_DIRNAME = 'watch'
_COMMAND_SUFFIX = '.command'
_LOG_SUFFIX = '.log'
_OFFSET_SUFFIX = '.offset'
_PID_SUFFIX = '.pid'


class WatchError(Exception):
  """a watch cannot be declared, found, or read; the message is operator-facing."""


def watch_dir() -> Path:
  directory = session_dir()
  if directory is None:
    raise WatchError(
      f'{SESSION_DIR_ENV} is unset: a watch keeps its lines in the session state dir'
    )
  return directory / WATCH_DIRNAME


def slug(command: list[str]) -> str:
  """the file stem a command's watch goes by: its words joined, anything a file
  name would not carry replaced by a dash."""
  return re.sub(r'[^A-Za-z0-9._]+', '-', ' '.join(command)).strip('-')


@dataclass(frozen=True)
class Watch:
  """one watched command and the files that carry it."""

  command: str
  directory: Path
  slug: str

  @property
  def log(self) -> Path:
    return self.directory / f'{self.slug}{_LOG_SUFFIX}'

  @property
  def offset_file(self) -> Path:
    return self.directory / f'{self.slug}{_OFFSET_SUFFIX}'

  @property
  def pid_file(self) -> Path:
    return self.directory / f'{self.slug}{_PID_SUFFIX}'

  def producer_alive(self) -> bool:
    """whether the `watch-run` feeding this watch's log is still running."""
    try:
      pid = int(self.pid_file.read_text().strip())
    except FileNotFoundError:
      return False
    try:
      os.kill(pid, 0)
    except ProcessLookupError:
      return False
    except PermissionError:
      return True
    return True

  def saved_offset(self) -> int:
    try:
      return int(self.offset_file.read_text().strip())
    except FileNotFoundError:
      return 0

  def read_new(self) -> tuple[list[str], int]:
    """the complete lines past the saved offset, and the offset after them."""
    offset = self.saved_offset()
    try:
      with self.log.open('rb') as log_file:
        log_file.seek(offset)
        data = log_file.read()
    except FileNotFoundError:
      return [], offset
    complete = data.rfind(b'\n') + 1
    lines = data[:complete].decode('utf-8', errors='replace').splitlines()
    return lines, offset + complete

  def save_offset(self, offset: int) -> None:
    staging = self.offset_file.with_name(f'{self.offset_file.name}.{os.getpid()}.tmp')
    staging.write_text(f'{offset}\n')
    os.replace(staging, self.offset_file)


def declare(command: list[str]) -> Watch:
  """register `command` as one of the session's watches, fed by this process,
  and return it. The pid lands before the command file, so a listed watch
  always names a producer."""
  if len(command) == 0:
    raise WatchError('a watch needs a command')
  directory = watch_dir()
  watch = Watch(command=shlex.join(command), directory=directory, slug=slug(command))
  if watch.producer_alive():
    raise WatchError(f'`{watch.command}` already runs in this session')
  directory.mkdir(parents=True, exist_ok=True)
  watch.pid_file.write_text(f'{os.getpid()}\n')
  (directory / f'{watch.slug}{_COMMAND_SUFFIX}').write_text(f'{watch.command}\n')
  return watch


def declared(command: Optional[list[str]] = None) -> list[Watch]:
  """the session's declared watches: the one for `command`, or every one; empty
  while none is declared."""
  directory = watch_dir()
  if command is not None:
    watch = Watch(command=shlex.join(command), directory=directory, slug=slug(command))
    if not (directory / f'{watch.slug}{_COMMAND_SUFFIX}').exists():
      return []
    return [watch]
  if not directory.is_dir():
    return []
  watches = []
  for command_file in sorted(directory.glob(f'*{_COMMAND_SUFFIX}')):
    stem = command_file.name[: -len(_COMMAND_SUFFIX)]
    watches.append(
      Watch(command=command_file.read_text().rstrip('\n'), directory=directory, slug=stem)
    )
  return watches
