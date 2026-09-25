#!/usr/bin/env python
"""`trails` — the operator surface over a recorded trail registry."""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import bro.base.args as base_args
from bro.trails import transfer
from bro.trails.store import (
  InvalidRequest,
  PermissionDenied,
  TrailHasForks,
  TrailNotFound,
  TrailsStore,
  UnsupportedOperation,
  default_store,
)

__cli_name__ = 'trails'


def _command_export(client: TrailsStore, args: dict[str, Any]) -> int:
  for imported in transfer.export_trails(client, args['trail_ids'], Path(args['output'])):
    print(f'{imported["trail_id"]}: {imported["extent"]} steps')
  return 0


def _command_import(client: TrailsStore, args: dict[str, Any]) -> int:
  for imported in transfer.import_layout(Path(args['directory']), client):
    print(f'{imported["trail_id"]}: {imported["extent"]} steps')
  return 0


def _command_migrate(client: TrailsStore, args: dict[str, Any]) -> int:
  for trail_id in args['trail_ids']:
    result = client.migrate_trail(trail_id)
    print(f'{trail_id}: format {result["format"]}, {result["migrated_rows"]} rows migrated')
  return 0


def _command_fold_contexts(client: TrailsStore, args: dict[str, Any]) -> int:
  if args['all'] == (len(args['trail_ids']) > 0):
    raise InvalidRequest('name --all or at least one trail id, but not both')
  operation_name = 'drop_context' if args['drop'] else 'fold_context'
  operation: Any = getattr(client, operation_name, None)
  if not callable(operation):
    raise UnsupportedOperation('this trails backend has no context administration surface')
  trail_ids = (
    (header['id'] for header in client.iter_trails()) if args['all'] else iter(args['trail_ids'])
  )
  for trail_id in trail_ids:
    result: Any = operation(trail_id, dry_run=args['dry_run'])
    if len(result['pointer_keys']) > 0:
      print(json.dumps(result, ensure_ascii=False, sort_keys=True))
  return 0


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

  export_parser = subparsers.add_parser(
    'export', help='write trails and their ancestry into a local store layout'
  )
  export_parser.add_argument(
    '-o', '--output', required=True, help='directory to write the store layout into'
  )
  export_parser.add_argument('trail_ids', nargs='+', help='trail ids to export')
  export_parser.set_handler(lambda **args: _dispatch(_command_export, args))

  import_parser = subparsers.add_parser(
    'import', help='import every trail a local store layout holds'
  )
  import_parser.add_argument('directory', help='store layout to import')
  import_parser.set_handler(lambda **args: _dispatch(_command_import, args))

  fold_parser = subparsers.add_parser(
    'fold-contexts', help='fold stored launch contexts into headers or drop their pointers'
  )
  fold_parser.add_argument('--dry-run', action='store_true', help='report without writing')
  fold_parser.add_argument(
    '--drop', action='store_true', help='drop pointers after checking the fold'
  )
  fold_parser.add_argument('--all', action='store_true', help='process every trail')
  fold_parser.add_argument('trail_ids', nargs='*', help='trail ids to process')
  fold_parser.set_handler(lambda **args: _dispatch(_command_fold_contexts, args))

  migrate_parser = subparsers.add_parser(
    'migrate', help='rewrite trails into the current schema format'
  )
  migrate_parser.add_argument('trail_ids', nargs='+', help='trail ids to migrate')
  migrate_parser.set_handler(lambda **args: _dispatch(_command_migrate, args))

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
      raise SystemExit(f'the trails credential is refused: {exception}') from exception
    except (InvalidRequest, UnsupportedOperation) as exception:
      raise SystemExit(f'the trails operation is refused: {exception}') from exception
