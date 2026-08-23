#!/usr/bin/env python
"""`trails` — the operator surface over a recorded trail registry."""

import sys
from collections.abc import Callable
from typing import Any, Optional

import bro.base.args as base_args
from bro.trails.store import (
  PermissionDenied,
  TrailHasForks,
  TrailNotFound,
  TrailsStore,
  default_store,
)

__cli_name__ = 'trails'


def _command_delete(client: TrailsStore, args: dict[str, Any]) -> int:
  """Exit 0 when every named trail went, 1 when any of them stayed.

  A trail is refused while a fork points at it, and an admin token reads no
  header, so a batch spanning a lineage cannot be ordered client-side; the passes
  are what make its order not matter.
  """
  pending = list(args['trail_ids'])
  refused: dict[str, Exception] = {}
  while True:
    refused = {}
    for trail_id in pending:
      try:
        removed = client.delete_trail(trail_id)
      except (TrailNotFound, TrailHasForks) as exception:
        refused[trail_id] = exception
        continue
      print(f'{trail_id}: {removed["extent"]} steps removed, manifest {removed["manifest"]}')
    if len(refused) == len(pending):
      break
    pending = list(refused)
  for exception in refused.values():
    print(exception, file=sys.stderr)
  return 1 if len(refused) > 0 else 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(description='administer a recorded trail registry')
  subparsers = parser.add_subparsers(dest='command')

  delete_parser = subparsers.add_parser(
    'delete', help='remove trails, writing a manifest of what goes'
  )
  delete_parser.add_argument('trail_ids', nargs='+', help='trail ids to remove, in any order')
  delete_parser.set_handler(lambda **args: _dispatch(_command_delete, args))

  return parser.dispatch(argv)


def _dispatch(command: Callable[[TrailsStore, dict[str, Any]], int], args: dict[str, Any]) -> int:
  with default_store() as client:
    try:
      return command(client, args)
    except PermissionDenied as exception:
      raise SystemExit(f'the trails credential administers nothing: {exception}') from exception
