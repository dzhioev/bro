"""run a command for the rest of the session and keep its lines for `watch-next`."""

import contextlib
import signal
import subprocess
import sys
from collections.abc import Generator

import bro.base.args as base_args
from bro import watches
from bro.base import log

__cli_name__ = 'watch-run'


def run(command: list[str]) -> int:
  """run `command` until it exits, appending each line it prints to the watch's
  log (and echoing it) and a closing `[watch-run] exited <code>` line after it."""
  watch = watches.declare(command)
  try:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, bufsize=1)
  except OSError as error:
    watch.pid_file.unlink(missing_ok=True)
    raise watches.WatchError(f'cannot run `{watch.command}`: {error}') from error

  assert process.stdout is not None
  with _forwarding_signals(process), watch.log.open('a') as log_file:
    for line in process.stdout:
      log_file.write(line)
      log_file.flush()
      sys.stdout.write(line)
      sys.stdout.flush()
    code = process.wait()
    log_file.write(f'[watch-run] exited {code}\n')
  watch.pid_file.unlink(missing_ok=True)
  return code


@contextlib.contextmanager
def _forwarding_signals(process: subprocess.Popen) -> Generator[None]:
  """forward SIGTERM and SIGINT to `process` for the block's duration."""

  def forward(signum, frame):
    del signum, frame
    process.terminate()

  previous = {number: signal.signal(number, forward) for number in (signal.SIGTERM, signal.SIGINT)}
  try:
    yield
  finally:
    for number, handler in previous.items():
      signal.signal(number, handler)


def main(argv: list[str]) -> int:
  parser = base_args.Parser(
    description='run a command for the rest of the session and keep its lines for `watch-next`'
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
