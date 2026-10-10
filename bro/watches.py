"""Lifetime-owned command producers and their one session-local output store."""

import codecs
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional, Self

from bro.base import spawn
from bro.base.text_window import BYTE_LIMIT, DEFAULT_LIMIT, format_size
from bro.monitor import SESSION_DIR_ENV, session_dir

WATCH_DIRNAME = 'watch'
OWNER_ENV = 'BRO_WATCH_OWNER'
PRODUCER_ENV = 'BRO_WATCH_PRODUCER'
PRODUCER_READY_FD_ENV = 'BRO_WATCH_READY_FD'
PRODUCER_OWNER_PATH_ENV = 'BRO_WATCH_OWNER_PATH'
PRODUCER_DIRECTORY_ENV = 'BRO_WATCH_DIRECTORY'
PRODUCER_SLUG_ENV = 'BRO_WATCH_SLUG'
PRODUCER_COMMAND_ENV = 'BRO_WATCH_COMMAND'
PRODUCER_POLICY_ENV = 'BRO_WATCH_BRASH_POLICY'
PRODUCER_JOURNAL_ENV = 'BRO_WATCH_JOURNAL'
SESSION_WATCH_COMMAND = 'quest watch'
_QUIET_MARK = b'\x1f'
_PENDING_MARKER = '[...pending watch lines...]'
_COMMAND_SUFFIX = '.command'
_LOG_SUFFIX = '.log'
_TIMES_SUFFIX = '.times'
_OFFSET_SUFFIX = '.offset'
_PID_SUFFIX = '.pid'
_JOURNAL_SUFFIX = '.journal'
_JOURNAL_HEAD_SUFFIX = '.journal-head'
_JOURNAL_WAKE_SUFFIX = '.journal-wake'
_WAKE_ON_QUIET_SUFFIX = '.wake-on-quiet'
_TURN_FILENAME = '.turn'
_NOTIFIED_FILENAME = '.notified'
_LOCK_FILENAME = '.lock'
_OWNER_FILENAME = '.owner'
_TERM_GRACE_SECONDS = 5.0
_COMMAND_TAG_BYTES = 1_024
_READ_CHUNK_BYTES = 65_536
_LAST_LINE_BYTES = 4_096
# one record of a watch's `.times` file: the offset in its log a line starts at,
# and when the line's first byte arrived, in epoch milliseconds
_ARRIVAL = struct.Struct('<QQ')
_LINE_BREAK_ESCAPES = {
  '\n': r'\n',
  '\r': r'\r',
  '\v': r'\v',
  '\f': r'\f',
  '\x1c': r'\x1c',
  '\x1d': r'\x1d',
  '\x1e': r'\x1e',
  '\x85': r'\x85',
  '\u2028': r'\u2028',
  '\u2029': r'\u2029',
}


class WatchError(Exception):
  """A watch cannot be declared, found, or controlled."""


def _visible(text: str) -> str:
  return ''.join(_LINE_BREAK_ESCAPES.get(character, character) for character in text)


def _command_tag(command: str) -> str:
  visible = _visible(command)
  encoded = visible.encode()
  if len(encoded) <= _COMMAND_TAG_BYTES:
    return f'[{visible}] '
  digest = hashlib.sha256(encoded).hexdigest()[:12]
  suffix = f' [...{format_size(len(encoded))}; sha256:{digest}]'
  prefix_bytes = encoded[: _COMMAND_TAG_BYTES - len(suffix.encode())]
  prefix = prefix_bytes.decode(errors='ignore')
  return f'[{prefix}{suffix}] '


def quiet(line: str) -> str:
  """Mark one output line quiet: it never wakes a session on its own and is
  delivered, unmarked, in the first batch a waking line brings."""
  return f'{_QUIET_MARK.decode()}{line}'


def _signal_journal_change(path: Path) -> None:
  try:
    file_descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
  except OSError as error:
    if error.errno in (errno.ENOENT, errno.ENXIO):
      return
    raise
  with os.fdopen(file_descriptor, 'wb', buffering=0) as wake:
    with contextlib.suppress(BrokenPipeError):
      wake.write(b'1')


def _nonblank(command: str) -> str:
  if len(command.strip()) == 0:
    raise WatchError('a watch needs a non-empty command')
  return command


def slug(command: str | list[str]) -> str:
  """Return a readable, collision-resistant file stem for one command."""
  value = shlex.join(command) if isinstance(command, list) else _nonblank(command)
  readable = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-')[:80] or 'watch'
  digest = hashlib.sha256(value.encode()).hexdigest()[:12]
  return f'{readable}-{digest}'


def _process_start_time(process_id: int) -> Optional[str]:
  stat = Path(f'/proc/{process_id}/stat')
  if stat.is_file():
    try:
      fields = stat.read_text().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
      return None
    if fields[0] == 'Z':
      return None
    return f'linux-ticks:{fields[19]}'
  process = subprocess.run(
    ['ps', '-o', 'state=,lstart=', '-p', str(process_id)],
    check=False,
    capture_output=True,
    text=True,
    start_new_session=True,
  )
  if process.returncode != 0:
    return None
  value = process.stdout.strip()
  if len(value) == 0 or value.split(maxsplit=1)[0].startswith('Z'):
    return None
  return f'ps:{value.split(maxsplit=1)[1]}'


@dataclass(frozen=True)
class ProcessIdentity:
  process_id: int
  start_time: str

  @classmethod
  def current(cls) -> Self:
    process_id = os.getpid()
    start_time = _process_start_time(process_id)
    if start_time is None:
      raise RuntimeError(f'cannot read start time for process {process_id}')
    return cls(process_id, start_time)

  @classmethod
  def read(cls, path: Path) -> Optional[Self]:
    try:
      value = json.loads(path.read_text())
    except FileNotFoundError:
      return None
    if not isinstance(value, dict) or set(value) != {'pid', 'start_time'}:
      raise WatchError(f'{path} carries a malformed producer identity')
    process_id = value['pid']
    start_time = value['start_time']
    if not isinstance(process_id, int) or not isinstance(start_time, str):
      raise WatchError(f'{path} carries a malformed producer identity')
    return cls(process_id, start_time)

  def write(self, path: Path) -> None:
    path.write_text(json.dumps({'pid': self.process_id, 'start_time': self.start_time}))

  def alive(self) -> bool:
    return _process_start_time(self.process_id) == self.start_time


@dataclass(frozen=True)
class Cursor:
  offset: int = 0
  line_size: Optional[int] = None
  line_remaining: Optional[int] = None
  quiet: bool = False
  # when the line the cursor stands inside arrived, in epoch seconds
  arrived: Optional[float] = None

  @classmethod
  def read(cls, path: Path) -> Self:
    try:
      value = json.loads(path.read_text())
    except FileNotFoundError:
      return cls()
    if not isinstance(value, dict) or not isinstance(value.get('offset'), int):
      raise WatchError(f'{path} carries a malformed watch offset')
    line_size = value.get('line_size')
    line_remaining = value.get('line_remaining')
    quiet = value.get('quiet', False)
    arrived = value.get('arrived')
    if not (line_size is None) == (line_remaining is None) == (arrived is None):
      raise WatchError(f'{path} carries a malformed watch offset')
    if not isinstance(quiet, bool) or (quiet and line_size is None):
      raise WatchError(f'{path} carries a malformed watch offset')
    if line_size is not None and (
      not isinstance(line_size, int)
      or not isinstance(line_remaining, int)
      or not isinstance(arrived, float)
      or line_size <= 0
      or line_remaining <= 0
    ):
      raise WatchError(f'{path} carries a malformed watch offset')
    return cls(value['offset'], line_size, line_remaining, quiet, arrived)

  def write(self, path: Path) -> None:
    staging = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    value: dict[str, int | float] = {'offset': self.offset}
    if self.line_size is not None:
      assert self.line_remaining is not None and self.arrived is not None
      value.update(
        line_size=self.line_size, line_remaining=self.line_remaining, arrived=self.arrived
      )
      if self.quiet:
        value['quiet'] = True
    staging.write_text(json.dumps(value))
    os.replace(staging, path)


@dataclass(frozen=True)
class Line:
  data: bytes
  size: int
  remaining: int
  start: int
  quiet: bool
  arrived: float


@dataclass(frozen=True)
class BatchLine:
  """One line of a batch: the command of the watch that produced it, with its
  line breaks escaped, None for a line no watch produced; what the line says;
  whether it wakes the session; and when its first byte arrived, in epoch
  seconds."""

  command: Optional[str]
  content: str
  wakes: bool
  arrived: float

  @property
  def text(self) -> str:
    """The line as the model reads it, tagged with its command."""
    return self.content if self.command is None else f'{_command_tag(self.command)}{self.content}'


@dataclass(frozen=True)
class Batch:
  """The lines one wake delivers, and whether complete lines wait past them."""

  lines: tuple[BatchLine, ...]
  pending: bool = False

  def text(self) -> str:
    """The batch as the model reads it."""
    rendered = [line.text for line in self.lines]
    if self.pending:
      rendered.append(_PENDING_MARKER)
    return '\n'.join(rendered)


@dataclass(frozen=True)
class Watch:
  command: str
  directory: Path
  slug: str

  @property
  def command_file(self) -> Path:
    return self.directory / f'{self.slug}{_COMMAND_SUFFIX}'

  @property
  def log(self) -> Path:
    return self.directory / f'{self.slug}{_LOG_SUFFIX}'

  @property
  def times(self) -> Path:
    return self.directory / f'{self.slug}{_TIMES_SUFFIX}'

  @property
  def offset_file(self) -> Path:
    return self.directory / f'{self.slug}{_OFFSET_SUFFIX}'

  @property
  def pid_file(self) -> Path:
    return self.directory / f'{self.slug}{_PID_SUFFIX}'

  @property
  def journal_file(self) -> Path:
    """The FIFO its command publishes journal heads through to its producer."""
    return self.directory / f'{self.slug}{_JOURNAL_SUFFIX}'

  @property
  def journal_head_file(self) -> Path:
    return self.directory / f'{self.slug}{_JOURNAL_HEAD_SUFFIX}'

  @property
  def journal_wake_file(self) -> Path:
    return self.directory / f'{self.slug}{_JOURNAL_WAKE_SUFFIX}'

  @property
  def wake_on_quiet_file(self) -> Path:
    return self.directory / f'{self.slug}{_WAKE_ON_QUIET_SUFFIX}'

  def wakes_on_quiet(self) -> bool:
    return self.wake_on_quiet_file.exists()

  def producer_identity(self) -> Optional[ProcessIdentity]:
    return ProcessIdentity.read(self.pid_file)

  def producer_alive(self) -> bool:
    identity = self.producer_identity()
    return identity is not None and identity.alive()

  def saved_offset(self) -> int:
    return Cursor.read(self.offset_file).offset

  def journal_head(self) -> Optional[int]:
    try:
      raw = self.journal_head_file.read_text()
    except FileNotFoundError:
      return None
    try:
      value = int(raw)
    except ValueError:
      raise WatchError(f'{self.journal_head_file} carries a malformed journal head') from None
    if value < 0 or raw != f'{value}\n':
      raise WatchError(f'{self.journal_head_file} carries a malformed journal head')
    return value

  def signal_journal_change(self) -> None:
    _signal_journal_change(self.journal_wake_file)

  def publish_journal_head(self, head: int) -> None:
    """Publish the journal head the watch's log has caught up with."""
    staging = self.journal_head_file.with_name(f'{self.journal_head_file.name}.{os.getpid()}.tmp')
    staging.write_text(f'{head}\n')
    os.replace(staging, self.journal_head_file)
    self.signal_journal_change()

  def last_complete_line(self) -> Optional[str]:
    """Return the bounded final complete line, or None when it is absent or wider."""
    try:
      with self.log.open('rb') as log_file:
        log_file.seek(0, os.SEEK_END)
        end = log_file.tell()
        start = max(0, end - _LAST_LINE_BYTES - 1)
        log_file.seek(start)
        data = log_file.read()
    except FileNotFoundError:
      return None
    if not data.endswith(b'\n'):
      data, separator, _partial = data.rpartition(b'\n')
      if len(separator) == 0:
        return None
    else:
      data = data[:-1]
    _previous, separator, line = data.rpartition(b'\n')
    if start > 0 and len(separator) == 0:
      return None
    return line.removeprefix(_QUIET_MARK).decode(errors='replace')

  def arrival(self, offset: int) -> float:
    """When the log line starting at `offset` began to arrive, in epoch seconds."""
    try:
      with self.times.open('rb') as records:
        low, high = 0, records.seek(0, os.SEEK_END) // _ARRIVAL.size
        while low < high:
          middle = (low + high) // 2
          records.seek(middle * _ARRIVAL.size)
          start, milliseconds = _ARRIVAL.unpack(records.read(_ARRIVAL.size))
          if start == offset:
            return milliseconds / 1000
          if start < offset:
            low = middle + 1
          else:
            high = middle
    except FileNotFoundError:
      pass
    raise WatchError(f'`{self.command}` log has no arrival time for its line at {offset}')

  def clear(self) -> None:
    for path in (
      self.command_file,
      self.log,
      self.times,
      self.offset_file,
      self.pid_file,
      self.journal_file,
      self.journal_head_file,
      self.journal_wake_file,
      self.wake_on_quiet_file,
    ):
      path.unlink(missing_ok=True)


class LineLog:
  """A producer's writer of its watch's log: what the command prints, each line's
  arrival recorded in the watch's `.times` before its first byte reaches the log."""

  def __init__(self, log: BinaryIO, records: BinaryIO) -> None:
    self._log = log
    self._records = records
    self._offset = log.seek(0, os.SEEK_END)
    self._at_line_start = self._offset == 0
    if not self._at_line_start:
      log.seek(self._offset - 1)
      self._at_line_start = log.read(1) == b'\n'

  @classmethod
  @contextlib.contextmanager
  def open(cls, watch: Watch) -> Generator[Self]:
    with (
      watch.log.open('a+b', buffering=0) as log,
      watch.times.open('ab', buffering=0) as records,
    ):
      yield cls(log, records)

  def write(self, data: bytes) -> None:
    position = 0
    while position < len(data):
      if self._at_line_start:
        self._records.write(_ARRIVAL.pack(self._offset, time.time_ns() // 1_000_000))
        self._at_line_start = False
      newline = data.find(b'\n', position)
      end = len(data) if newline < 0 else newline + 1
      piece = memoryview(data)[position:end]
      while len(piece) > 0:
        piece = piece[self._log.write(piece) :]
      self._offset += end - position
      self._at_line_start = newline >= 0
      position = end

  def end_line(self) -> None:
    """Close the line the command left without its line break, if it did."""
    if not self._at_line_start:
      self.write(b'\n')


class Store:
  """The declarations, logs, and one committed reader cursor for a session."""

  def __init__(self, directory: Path, owner_path: Path):
    self.directory = directory
    self.owner_path = owner_path

  @contextlib.contextmanager
  def _locked(self) -> Generator[None]:
    self.directory.mkdir(parents=True, exist_ok=True)
    with (self.directory / _LOCK_FILENAME).open('a') as lock_file:
      fcntl.flock(lock_file, fcntl.LOCK_EX)
      yield

  def _watch(self, command: str) -> Watch:
    return Watch(_nonblank(command), self.directory, slug(command))

  def _declared_locked(self) -> list[Watch]:
    if not self.directory.is_dir():
      return []
    result = []
    for command_file in sorted(self.directory.glob(f'*{_COMMAND_SUFFIX}')):
      stem = command_file.name[: -len(_COMMAND_SUFFIX)]
      result.append(Watch(command_file.read_text(), self.directory, stem))
    return result

  def declared(self) -> list[Watch]:
    with self._locked():
      return self._declared_locked()

  def has_waking_lines(self) -> bool:
    """Whether any declared watch has a complete line past its committed cursor
    that wakes the session."""
    with self._locked():
      return self._has_waking_line_locked(self._declared_locked())

  def set_wake_on_quiet(self, command: str, wake: bool) -> None:
    """Set whether the watch's quiet lines wake the session like any other line."""
    watch = self._watch(command)
    with self._locked():
      if not watch.command_file.exists():
        raise WatchError(f'no watch runs `{watch.command}` in this session')
      if wake:
        watch.wake_on_quiet_file.touch()
      else:
        watch.wake_on_quiet_file.unlink(missing_ok=True)

  def mark_notified(self, live_set: frozenset[str]) -> bool:
    """Record a live set and return whether this is its first notice."""
    encoded = json.dumps(sorted(live_set), ensure_ascii=False, separators=(',', ':')).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    path = self.directory / _NOTIFIED_FILENAME
    with self._locked():
      try:
        entries = path.read_text().splitlines()
      except FileNotFoundError:
        entries = []
      if any(re.fullmatch(r'[0-9a-f]{64}', entry) is None for entry in entries):
        raise WatchError(f'{path} carries malformed notice memory')
      seen = set(entries)
      if digest in seen:
        return False
      with path.open('a') as notified:
        notified.write(f'{digest}\n')
      return True

  def wait_for_journal_head(self, command: str, target: int) -> bool:
    """Block for a producer to publish `target`, or return false when it ends first."""
    if not isinstance(target, int) or isinstance(target, bool) or target < 0:
      raise ValueError('journal target must be a non-negative integer')
    watch = self._watch(command)
    while True:
      published = watch.journal_head()
      if published is not None and published >= target:
        return True
      identity = watch.producer_identity()
      if identity is None or not identity.alive():
        return False
      try:
        wake_fd = os.open(watch.journal_wake_file, os.O_RDWR | os.O_NONBLOCK)
      except FileNotFoundError:
        raise WatchError(f'`{watch.command}` has no journal wake handle') from None
      with contextlib.ExitStack() as cleanup:
        cleanup.callback(os.close, wake_fd)
        published = watch.journal_head()
        if published is not None and published >= target:
          return True
        if not identity.alive():
          return False
        pidfd_open = getattr(os, 'pidfd_open', None)
        if pidfd_open is None:
          os.set_blocking(wake_fd, True)
          os.read(wake_fd, 1)
          continue
        try:
          process_fd = pidfd_open(identity.process_id)
        except ProcessLookupError:
          continue
        cleanup.callback(os.close, process_fd)
        ready, _, _ = select.select([wake_fd, process_fd], [], [])
        if wake_fd in ready:
          os.read(wake_fd, 1)

  def start(
    self, command: str, policy: Optional[Path] = None, *, wake_on_quiet: bool = False
  ) -> Watch:
    """start `command` as a producer: in brash under `policy`, or in bash where
    there is none."""
    watch = self._watch(command)
    with self._locked():
      if watch.producer_alive():
        raise WatchError(f'`{watch.command}` already runs in this session')
      watch.clear()
      watch.command_file.write_text(watch.command)
      if wake_on_quiet:
        watch.wake_on_quiet_file.touch()
      os.mkfifo(watch.journal_file)
      os.mkfifo(watch.journal_wake_file)
      ready_read_fd, ready_write_fd = os.pipe()
      environment = {
        **os.environ,
        PRODUCER_ENV: '1',
        PRODUCER_READY_FD_ENV: str(ready_write_fd),
        PRODUCER_OWNER_PATH_ENV: str(self.owner_path),
        PRODUCER_DIRECTORY_ENV: str(self.directory),
        PRODUCER_SLUG_ENV: watch.slug,
        PRODUCER_COMMAND_ENV: watch.command,
        PRODUCER_JOURNAL_ENV: str(watch.journal_file),
      }
      if policy is None:
        environment.pop(PRODUCER_POLICY_ENV, None)
      else:
        environment[PRODUCER_POLICY_ENV] = str(policy)
      process: Optional[subprocess.Popen] = None
      try:
        with (
          os.fdopen(ready_read_fd, 'rb') as ready,
          os.fdopen(ready_write_fd, 'wb') as ready_writer,
        ):
          process = subprocess.Popen(
            spawn.module_argv('bro.watch_run'),
            env=environment,
            pass_fds=(ready_write_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
          )
          ready_writer.close()
          started = ready.read(1)
      except OSError as error:
        if process is not None:
          with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
          process.wait()
        watch.clear()
        raise WatchError(f'cannot start `{watch.command}`: {error}') from error
      assert process is not None
      if started != b'1':
        process.wait()
        watch.clear()
        raise WatchError(f'watch producer failed to start `{watch.command}`')
      threading.Thread(target=process.wait, daemon=True).start()
      return watch

  def _terminate(self, watch: Watch) -> None:
    identity = watch.producer_identity()
    if identity is None or not identity.alive():
      return
    pidfd_open = getattr(os, 'pidfd_open', None)
    if pidfd_open is None:
      self._terminate_without_pidfd(identity)
      return
    try:
      process_fd = pidfd_open(identity.process_id)
    except ProcessLookupError:
      return
    with os.fdopen(process_fd):
      try:
        os.killpg(identity.process_id, signal.SIGTERM)
      except ProcessLookupError:
        return
      if len(select.select([process_fd], [], [], _TERM_GRACE_SECONDS)[0]) == 0:
        with contextlib.suppress(ProcessLookupError):
          os.killpg(identity.process_id, signal.SIGKILL)
        select.select([process_fd], [], [])

  @staticmethod
  def _terminate_without_pidfd(identity: ProcessIdentity) -> None:
    try:
      os.killpg(identity.process_id, signal.SIGTERM)
    except ProcessLookupError:
      return
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while identity.alive() and time.monotonic() < deadline:
      time.sleep(0.05)
    if identity.alive():
      with contextlib.suppress(ProcessLookupError):
        os.killpg(identity.process_id, signal.SIGKILL)
    while identity.alive():
      time.sleep(0.05)

  def stop(self, command: str, *, session: bool = False) -> None:
    watch = self._watch(command)
    if watch.command == SESSION_WATCH_COMMAND and not session:
      raise WatchError('the session watch is owned by the runtime and cannot be stopped')
    with self._locked():
      if not watch.command_file.exists():
        raise WatchError(f'no watch runs `{watch.command}` in this session')
      self._terminate(watch)
      watch.clear()

  def stop_all(self) -> None:
    with self._locked():
      for watch in self._declared_locked():
        self._terminate(watch)

  def reset(self) -> None:
    if self.directory.exists():
      self.stop_all()
      shutil.rmtree(self.directory)
    self.directory.mkdir(parents=True)

  def _ordered_locked(self) -> list[Watch]:
    watches = self._declared_locked()
    if len(watches) < 2:
      return watches
    try:
      last = (self.directory / _TURN_FILENAME).read_text().strip()
    except FileNotFoundError:
      return watches
    for index, watch in enumerate(watches):
      if watch.slug == last:
        return watches[index + 1 :] + watches[: index + 1]
    return watches

  @staticmethod
  def _read_line(watch: Watch, cursor: Cursor) -> Optional[Line]:
    try:
      with watch.log.open('rb') as log_file:
        log_file.seek(cursor.offset)
        if cursor.line_remaining is not None:
          assert cursor.line_size is not None
          read_size = min(cursor.line_remaining, BYTE_LIMIT + 4)
          data = log_file.read(read_size)
          if len(data) != read_size:
            raise WatchError(f'`{watch.command}` log was truncated behind its committed offset')
          if cursor.line_remaining == read_size and log_file.read(1) != b'\n':
            raise WatchError(f'`{watch.command}` log changed behind its committed offset')
          assert cursor.arrived is not None
          return Line(
            data,
            cursor.line_size,
            cursor.line_remaining,
            cursor.offset,
            cursor.quiet,
            cursor.arrived,
          )

        retained = bytearray()
        size = 0
        while True:
          chunk = log_file.read(_READ_CHUNK_BYTES)
          if len(chunk) == 0:
            return None
          newline = chunk.find(b'\n')
          content = chunk if newline < 0 else chunk[:newline]
          size += len(content)
          if len(retained) < BYTE_LIMIT + 4:
            keep = min(len(content), BYTE_LIMIT + 4 - len(retained))
            retained.extend(content[:keep])
          if newline >= 0:
            arrived = watch.arrival(cursor.offset)
            if retained.startswith(_QUIET_MARK):
              mark_size = len(_QUIET_MARK)
              data = bytes(retained[mark_size:])
              remaining = size - mark_size
              return Line(data, remaining, remaining, cursor.offset + mark_size, True, arrived)
            return Line(bytes(retained), size, size, cursor.offset, False, arrived)
    except FileNotFoundError:
      return None

  @classmethod
  def _decoded_prefix(cls, data: bytes, limit: int, *, final: bool) -> tuple[str, int]:
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    parts: list[str] = []
    used_bytes = 0
    consumed = 0
    pending_start = 0
    for index, byte in enumerate(data):
      decoded = decoder.decode(bytes((byte,)), final=False)
      if len(decoded) == 0:
        continue
      visible = _visible(decoded)
      size = len(visible.encode())
      if used_bytes + size > limit:
        return ''.join(parts), pending_start
      parts.append(visible)
      used_bytes += size
      buffered_bytes = len(decoder.getstate()[0])
      consumed = index + 1 - buffered_bytes
      pending_start = consumed
    if final and pending_start < len(data):
      visible = _visible(decoder.decode(b'', final=True))
      size = len(visible.encode())
      if used_bytes + size <= limit:
        parts.append(visible)
        consumed = len(data)
    return ''.join(parts), consumed

  @classmethod
  def _line_fits(cls, line: Line, fixed: str, available: int) -> bool:
    if line.remaining > len(line.data):
      return False
    payload_bytes = available - len(fixed.encode()) - 1
    if payload_bytes < 0:
      return False
    _, consumed = cls._decoded_prefix(line.data, payload_bytes, final=True)
    return consumed == line.remaining

  @classmethod
  def _has_complete_line(cls, watch: Watch, cursor: Cursor) -> bool:
    return cls._read_line(watch, cursor) is not None

  @classmethod
  def _has_waking_line(cls, watch: Watch, cursor: Cursor) -> bool:
    if watch.wakes_on_quiet():
      return cls._has_complete_line(watch, cursor)
    start = cursor.offset
    if cursor.line_remaining is not None:
      if not cursor.quiet:
        return True
      start += cursor.line_remaining + 1
    try:
      with watch.log.open('rb') as log_file:
        log_file.seek(start)
        line_quiet: Optional[bool] = None
        while True:
          chunk = log_file.read(_READ_CHUNK_BYTES)
          if len(chunk) == 0:
            return False
          position = 0
          while position < len(chunk):
            if line_quiet is None:
              line_quiet = chunk.startswith(_QUIET_MARK, position)
            newline = chunk.find(b'\n', position)
            if newline < 0:
              break
            if not line_quiet:
              return True
            line_quiet = None
            position = newline + 1
    except FileNotFoundError:
      return False

  @classmethod
  def _has_waking_line_locked(cls, declared: list[Watch]) -> bool:
    return any(cls._has_waking_line(watch, Cursor.read(watch.offset_file)) for watch in declared)

  @classmethod
  def _pending_after(
    cls,
    ordered: list[Watch],
    watch_index: int,
    watch: Watch,
    cursor: Cursor,
  ) -> bool:
    if cls._has_complete_line(watch, cursor):
      return True
    return any(
      cls._has_complete_line(candidate, Cursor.read(candidate.offset_file))
      for candidate in ordered[watch_index + 1 :]
    )

  def take(self) -> Optional[Batch]:
    """Commit and return the next bounded, fair batch, or None while no complete
    line that wakes the session waits."""
    marker_bytes = len(f'{_PENDING_MARKER}\n'.encode())
    byte_budget = BYTE_LIMIT - marker_bytes
    line_budget = DEFAULT_LIMIT - 1
    pieces: list[BatchLine] = []
    pending = False
    used_bytes = 0
    cut_watch: Optional[Watch] = None
    last_served_watch: Optional[Watch] = None

    with self._locked():
      ordered = self._ordered_locked()
      if not self._has_waking_line_locked(ordered):
        return None
      for watch_index, watch in enumerate(ordered):
        cursor = Cursor.read(watch.offset_file)
        while True:
          if len(pieces) >= line_budget or used_bytes >= byte_budget:
            if self._pending_after(ordered, watch_index, watch, cursor):
              cut_watch = last_served_watch
            break
          line = self._read_line(watch, cursor)
          if line is None:
            break

          tag = _command_tag(watch.command)
          continuing = cursor.line_remaining is not None
          wide = continuing or not self._line_fits(line, tag, byte_budget)
          size_marker = f'[line: {format_size(line.size)}] ' if wide and not continuing else ''
          fixed = f'{tag}{size_marker}'
          available = byte_budget - used_bytes - len(fixed.encode()) - 1
          if available <= 0:
            cut_watch = last_served_watch
            break

          text, content_consumed = self._decoded_prefix(
            line.data,
            available,
            final=line.remaining <= len(line.data),
          )
          if content_consumed < line.remaining and not wide:
            cut_watch = last_served_watch
            break
          if content_consumed == 0 and line.remaining > 0:
            if last_served_watch is None:
              raise WatchError(f'`{watch.command}` cannot fit one output character in a batch')
            cut_watch = last_served_watch
            break

          remaining = line.remaining - content_consumed
          if remaining == 0:
            next_cursor = Cursor(line.start + content_consumed + 1)
          else:
            next_cursor = Cursor(
              line.start + content_consumed, line.size, remaining, line.quiet, line.arrived
            )
          content = f'{size_marker}{text}'
          wakes = not line.quiet or watch.wakes_on_quiet()
          pieces.append(BatchLine(_visible(watch.command), content, wakes, line.arrived))
          used_bytes += len(f'{tag}{content}'.encode()) + 1
          cursor = next_cursor
          cursor.write(watch.offset_file)
          last_served_watch = watch

          if len(pieces) >= line_budget or used_bytes >= byte_budget:
            if self._pending_after(ordered, watch_index, watch, cursor):
              cut_watch = watch
            break
        if cut_watch is not None:
          break

      if cut_watch is not None:
        (self.directory / _TURN_FILENAME).write_text(f'{cut_watch.slug}\n')
        pending = self._has_waking_line_locked(ordered)

    if len(pieces) == 0:
      return None
    batch = Batch(tuple(pieces), pending)
    text = batch.text()
    if len(text.splitlines()) > DEFAULT_LIMIT or len(text.encode()) > BYTE_LIMIT:
      raise RuntimeError('watch batch exceeded the shared output bounds')
    return batch


class Owner:
  """The liveness handle and teardown scope for every producer in one store."""

  def __init__(
    self,
    store: Store,
    temporary_directory: Optional[tempfile.TemporaryDirectory] = None,
    *,
    publish_environment: bool = True,
  ):
    self.store = store
    self._temporary_directory = temporary_directory
    self._publish_environment = publish_environment
    self._handle: Optional[int] = None
    self._previous_environment: Optional[str] = None

  @classmethod
  def for_session(cls) -> Self:
    state = session_dir()
    if state is None:
      raise WatchError(f'{SESSION_DIR_ENV} is unset: a session watch needs a session state dir')
    directory = state / WATCH_DIRNAME
    return cls(Store(directory, directory / _OWNER_FILENAME))

  @classmethod
  def temporary(cls, *, publish_environment: bool = True) -> Self:
    temporary_directory = tempfile.TemporaryDirectory(prefix='bro-watches-')
    directory = Path(temporary_directory.name) / WATCH_DIRNAME
    return cls(
      Store(directory, directory / _OWNER_FILENAME),
      temporary_directory,
      publish_environment=publish_environment,
    )

  def __enter__(self) -> Self:
    self.store.reset()
    os.mkfifo(self.store.owner_path)
    self._handle = os.open(self.store.owner_path, os.O_RDWR | os.O_NONBLOCK)
    if self._publish_environment:
      self._previous_environment = os.environ.get(OWNER_ENV)
      os.environ[OWNER_ENV] = str(self.store.owner_path)
    return self

  def __exit__(self, *_exception_info) -> None:
    with contextlib.ExitStack() as cleanup:
      if self._temporary_directory is not None:
        cleanup.callback(self._temporary_directory.cleanup)
      if self._publish_environment:
        cleanup.callback(self._restore_environment)
      if self._handle is not None:
        handle = self._handle
        self._handle = None
        cleanup.callback(os.close, handle)
      self.store.stop_all()

  def _restore_environment(self) -> None:
    if self._previous_environment is None:
      os.environ.pop(OWNER_ENV, None)
    else:
      os.environ[OWNER_ENV] = self._previous_environment


def session_store() -> Store:
  directory = watch_dir()
  owner_value = os.environ.get(OWNER_ENV)
  if owner_value is None:
    raise WatchError(f'{OWNER_ENV} is unset: the session does not own watch producers')
  return Store(directory, Path(owner_value))


def watch_dir() -> Path:
  state = session_dir()
  if state is None:
    raise WatchError(
      f'{SESSION_DIR_ENV} is unset: a watch keeps its lines in the session state dir'
    )
  return state / WATCH_DIRNAME


def publish_journal_head(head: int) -> None:
  """Publish the ordered journal head after a watched stream emitted through it.

  The head goes to the stream's producer, which publishes it once the output
  written before it is in the watch's log."""
  if not isinstance(head, int) or isinstance(head, bool) or head < 0:
    raise ValueError('journal head must be a non-negative integer')
  raw_path = os.environ.get(PRODUCER_JOURNAL_ENV)
  if raw_path is None:
    return
  journal = os.open(raw_path, os.O_WRONLY | os.O_NONBLOCK)
  with os.fdopen(journal, 'wb', buffering=0) as writer:
    os.set_blocking(journal, True)
    writer.write(f'{head}\n'.encode())


def session_watch_admitted(
  *,
  may_summon: Optional[tuple[str, ...]] = None,
  summoned: Optional[bool] = None,
  talk: Optional[tuple[str, ...]] = None,
) -> bool:
  """Whether this session can receive traffic carried only by its quest watch."""
  from bro import summon

  resolved_may_summon = summon.effective_may_summon() if may_summon is None else may_summon
  resolved_summoned = summon.summoned() if summoned is None else summoned
  resolved_talk = summon.talk() if talk is None else talk
  quest_traffic = resolved_talk is not None and any(
    right in resolved_talk for right in ('owner.say', 'owner.question', 'worker.question')
  )
  return len(resolved_may_summon) > 0 or (resolved_summoned and quest_traffic)
