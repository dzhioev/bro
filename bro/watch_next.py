"""deliver the next lines of the session's watches, blocking until there are some."""

import shlex
import sys
import time
from typing import Optional, TextIO

import bro.base.args as base_args
from bro import watches
from bro.base import log

__cli_name__ = 'watch-next'

POLL_SECONDS = 0.5
# how long a wait started beside its `watch-run` gives the declaration to land
DECLARATION_GRACE_SECONDS = 10.0


def _declared_within(
  command: Optional[list[str]], grace_seconds: float, poll_seconds: float
) -> list[watches.Watch]:
  deadline = time.monotonic() + grace_seconds
  while True:
    targets = watches.declared(command)
    if len(targets) > 0:
      return targets
    if time.monotonic() >= deadline:
      if command is None:
        raise watches.WatchError(
          'no watch runs in this session; start one with `watch-run <command>`'
        )
      raise watches.WatchError(
        f'no watch runs `{shlex.join(command)}` in this session; start it with '
        f'`watch-run {shlex.join(command)}`'
      )
    time.sleep(poll_seconds)


def wait(
  command: Optional[list[str]],
  out: TextIO,
  *,
  poll_seconds: float = POLL_SECONDS,
  declaration_grace_seconds: float = DECLARATION_GRACE_SECONDS,
) -> int:
  """block until one of the watches (the one running `command`, or every one)
  has lines past its offset, print them tagged by watch, and record the offset
  once they are out; refuse once every watch has ended with nothing left, or
  when none is declared within the grace."""
  targets = _declared_within(command, declaration_grace_seconds, poll_seconds)
  while True:
    alive = any(watch.producer_alive() for watch in targets)
    delivered = False
    for watch in targets:
      lines, offset = watch.read_new()
      if len(lines) == 0:
        continue
      for line in lines:
        out.write(f'[{watch.command}] {line}\n')
      out.flush()
      watch.save_offset(offset)
      delivered = True
    if delivered:
      return 0
    if not alive:
      ended = ', '.join(f'`{watch.command}`' for watch in targets)
      raise watches.WatchError(f'every watch has ended: {ended}')
    time.sleep(poll_seconds)


def main(argv: list[str]) -> int:
  parser = base_args.Parser(
    description="deliver the next lines of the session's watches, blocking until there are some"
  )
  parser.add_argument(
    'command',
    nargs=base_args.REMAINDER,
    metavar='COMMAND',
    help='the watched command to read; every watch when omitted',
  )
  arguments = parser.parse(argv)
  command = arguments['command']
  try:
    return wait(command if len(command) > 0 else None, sys.stdout)
  except watches.WatchError as error:
    log.error('%s', error)
    return 1


if __name__ == '__main__':
  sys.exit(main(sys.argv))
