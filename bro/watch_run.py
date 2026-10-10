"""Start a detached, lifetime-owned producer for one watched command line."""

import contextlib
import fcntl
import os
import select
import shlex
import struct
import sys
import termios
import threading
from pathlib import Path
from typing import Optional

import bro.base.args as base_args
from bro import brash_policy, job_supervisor, watches
from bro.base import log

__cli_name__ = 'watch-run'

_CHUNK_BYTES = 65_536


def run(command: list[str]) -> int:
  watches.session_store().start(shlex.join(command))
  return 0


def _required_environment(name: str) -> str:
  value = os.environ.get(name)
  if value is None:
    raise RuntimeError(f'{name} is required for a watch producer')
  return value


class _Copier:
  """The thread copying a command's output into its watch's log as it arrives,
  and publishing each journal head the command sends once the output it wrote
  before that head is in the log."""

  def __init__(
    self, watch: watches.Watch, line_log: watches.LineLog, output: int, journal: int
  ) -> None:
    self._watch = watch
    self._line_log = line_log
    self._output = output
    self._journal = journal
    self._error: Optional[BaseException] = None
    self._thread = threading.Thread(target=self._run, daemon=True)
    self._thread.start()

  def _run(self) -> None:
    try:
      self._copy()
    except BaseException as error:
      self._error = error
      # the command would block writing into a pipe nobody reads any more
      job_supervisor.end_group()

  def _copy(self) -> None:
    os.set_blocking(self._output, False)
    os.set_blocking(self._journal, False)
    heads = b''
    while True:
      select.select([self._output, self._journal], [], [])
      # the output queue is sampled after the heads are read, so it holds all
      # the output their command wrote before sending them; it is copied as
      # sampled, so output that keeps coming cannot hold the heads back
      heads += _read(self._journal, _queued(self._journal))
      ended = not self._copy_output(_queued(self._output))
      heads = self._published(heads)
      if ended:
        self._published(heads + _read(self._journal, _queued(self._journal)))
        return

  def _copy_output(self, queued: int) -> bool:
    """Copy `queued` bytes of output, or those of them before its end; False
    once the output has ended."""
    # one read at least, which sees the end of an output with nothing queued
    remaining = max(queued, 1)
    while remaining > 0:
      try:
        chunk = os.read(self._output, min(remaining, _CHUNK_BYTES))
      except BlockingIOError:
        return True
      if len(chunk) == 0:
        return False
      self._line_log.write(chunk)
      remaining -= len(chunk)
    return True

  def _published(self, heads: bytes) -> bytes:
    """Publish the highest complete head in `heads` and return what follows it."""
    *complete, partial = heads.split(b'\n')
    if len(complete) > 0:
      self._watch.publish_journal_head(max(int(head) for head in complete))
    return partial

  def finish(self) -> None:
    """Wait for the output's end, raising what the copy raised."""
    self._thread.join()
    if self._error is not None:
      raise self._error


def _queued(descriptor: int) -> int:
  """How many bytes the pipe `descriptor` reads from holds now."""
  return struct.unpack('i', fcntl.ioctl(descriptor, termios.FIONREAD, bytes(4)))[0]


def _read(descriptor: int, count: int) -> bytes:
  """Read the `count` bytes the pipe `descriptor` reads from holds."""
  data = bytearray()
  while len(data) < count:
    chunk = os.read(descriptor, count - len(data))
    if len(chunk) == 0:
      raise RuntimeError('a pipe ended before the bytes it held were read')
    data.extend(chunk)
  return bytes(data)


def _produce() -> int:
  ready_fd = int(_required_environment(watches.PRODUCER_READY_FD_ENV))
  owner_path = Path(_required_environment(watches.PRODUCER_OWNER_PATH_ENV))
  directory = Path(_required_environment(watches.PRODUCER_DIRECTORY_ENV))
  watch = watches.Watch(
    command=_required_environment(watches.PRODUCER_COMMAND_ENV),
    directory=directory,
    slug=_required_environment(watches.PRODUCER_SLUG_ENV),
  )
  policy_value = os.environ.get(watches.PRODUCER_POLICY_ENV)
  policy = None if policy_value is None else Path(policy_value)
  identity = watches.ProcessIdentity.current()
  identity.write(watch.pid_file)
  owner_fd = os.open(owner_path, os.O_RDONLY | os.O_NONBLOCK)
  os.set_blocking(owner_fd, True)

  def clear_identity() -> None:
    current = watch.producer_identity()
    if current == identity:
      watch.pid_file.unlink(missing_ok=True)
    watch.signal_journal_change()

  read_fd, write_fd = os.pipe()
  with (
    contextlib.ExitStack() as cleanup,
    os.fdopen(read_fd, 'rb', buffering=0) as source,
    os.fdopen(write_fd, 'wb', buffering=0) as output,
    watches.LineLog.open(watch) as line_log,
    os.fdopen(os.open(watch.journal_file, os.O_RDONLY | os.O_NONBLOCK), 'rb', 0) as journal,
    # held open, so the journal never reads as ended between the command's heads
    open(watch.journal_file, 'wb', buffering=0),
  ):
    cleanup.callback(clear_identity)
    copier = _Copier(watch, line_log, source.fileno(), journal.fileno())

    def record_exit(code: int) -> None:
      # the command and its descendants are gone; with the producer's own write
      # end closed too, the copier reads the output to its end
      output.close()
      copier.finish()
      line_log.end_line()
      line_log.write(f'[watch-run] exited {code}\n'.encode())
      clear_identity()

    return job_supervisor.supervise(
      brash_policy.line_argv(watch.command, policy),
      ready_fd,
      owner_fd,
      output=output,
      finished=record_exit,
    )


def main(argv: list[str]) -> int:
  if os.environ.get(watches.PRODUCER_ENV) == '1':
    return _produce()
  parser = base_args.Parser(
    description='start a shell command as a detached producer for this session watch store'
  )
  parser.add_argument(
    'command',
    nargs=base_args.REMAINDER,
    metavar='COMMAND',
    help='the command and its arguments',
  )
  arguments = parser.parse(argv)
  command = arguments['command']
  if len(command) == 0:
    parser.error('a command is required')
  try:
    return run(command)
  except watches.WatchError as error:
    log.error('%s', error)
    return 1


if __name__ == '__main__':
  sys.exit(main(sys.argv))
