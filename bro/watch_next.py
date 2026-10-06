"""Deliver the next bounded batch from the session's one watch store."""

import shlex
import sys
import time
from typing import Optional, TextIO

import bro.base.args as base_args
from bro import watches
from bro.base import log

__cli_name__ = 'watch-next'
_DECLARATION_GRACE_SECONDS = 1.0
_POLL_SECONDS = 0.2


def _targets(store: watches.Store, command: Optional[list[str]]) -> list[watches.Watch]:
  if command is None:
    return store.declared()
  return store.declared(shlex.join(command))


def wait(
  command: Optional[list[str]],
  out: TextIO,
  *,
  poll_seconds: float = _POLL_SECONDS,
  declaration_grace_seconds: float = _DECLARATION_GRACE_SECONDS,
) -> int:
  """Block until the unified store has a batch, or every selected producer ended."""
  store = watches.session_store()
  deadline = time.monotonic() + declaration_grace_seconds
  targets = _targets(store, command)
  while len(targets) == 0 and time.monotonic() < deadline:
    time.sleep(poll_seconds)
    targets = _targets(store, command)
  if len(targets) == 0:
    if command is None:
      raise watches.WatchError(
        'no watch runs in this session; start one with `watch-run <command>`'
      )
    rendered = shlex.join(command)
    raise watches.WatchError(
      f'no watch runs `{rendered}` in this session; start it with `watch-run {rendered}`'
    )

  while True:
    batch = store.take(shlex.join(command) if command is not None else None)
    if batch is not None:
      out.write(f'{batch}\n')
      out.flush()
      return 0
    targets = _targets(store, command)
    if not any(watch.producer_alive() for watch in targets):
      ended = ', '.join(f'`{watch.command}`' for watch in targets)
      raise watches.WatchError(f'every watch has ended: {ended}')
    time.sleep(poll_seconds)


def main(argv: list[str]) -> int:
  parser = base_args.Parser(
    description='deliver the next bounded batch from the session watch store'
  )
  parser.add_argument(
    'command',
    nargs=base_args.REMAINDER,
    default=None,
    help='the watched command to await; every watch when omitted',
  )
  arguments = parser.parse(argv)
  command = arguments['command'] or None
  try:
    return wait(command, sys.stdout)
  except watches.WatchError as error:
    log.error('%s', error)
    return 1


if __name__ == '__main__':
  sys.exit(main(sys.argv))
