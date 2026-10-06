"""Start a detached, lifetime-owned producer for one watched shell command."""

import contextlib
import os
import shlex
import sys
from pathlib import Path

import bro.base.args as base_args
from bro import job_supervisor, watches
from bro.base import log

__cli_name__ = 'watch-run'


def run(command: list[str]) -> int:
  watches.session_store().start(shlex.join(command))
  return 0


def _required_environment(name: str) -> str:
  value = os.environ.get(name)
  if value is None:
    raise RuntimeError(f'{name} is required for a watch producer')
  return value


def _produce() -> int:
  ready_fd = int(_required_environment(watches.PRODUCER_READY_FD_ENV))
  owner_path = Path(_required_environment(watches.PRODUCER_OWNER_PATH_ENV))
  directory = Path(_required_environment(watches.PRODUCER_DIRECTORY_ENV))
  watch = watches.Watch(
    command=_required_environment(watches.PRODUCER_COMMAND_ENV),
    directory=directory,
    slug=_required_environment(watches.PRODUCER_SLUG_ENV),
  )
  identity = watches.ProcessIdentity.current()
  identity.write(watch.pid_file)
  owner_fd = os.open(owner_path, os.O_RDONLY | os.O_NONBLOCK)
  os.set_blocking(owner_fd, True)

  def clear_identity() -> None:
    current = watch.producer_identity()
    if current == identity:
      watch.pid_file.unlink(missing_ok=True)

  with contextlib.ExitStack() as cleanup:
    cleanup.callback(clear_identity)
    with watch.log.open('ab', buffering=0) as log_file:

      def record_exit(code: int) -> None:
        log_file.write(f'[watch-run] exited {code}\n'.encode())
        clear_identity()

      return job_supervisor.supervise(
        watch.command,
        ready_fd,
        owner_fd,
        output=log_file,
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
