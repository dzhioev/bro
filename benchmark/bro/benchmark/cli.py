#!/usr/bin/env python
"""`benchmark` — post-run benchmark workflows."""

import sys
from pathlib import Path
from typing import Optional

import bro.base.args as base_args
from bro import artifact
from bro.base import credentials, log
from bro.benchmark import bundle, import_trails, publication, query, retention
from bro.trails.store import configured_store

__cli_name__ = 'benchmark'


def _retain(source: str) -> int:
  try:
    retained = retention.retain_job(retention.resolve_job(source))
  except (
    artifact.ArtifactError,
    credentials.SecretNotFound,
    OSError,
    ValueError,
    retention.RetentionError,
  ) as error:
    log.error('benchmark retain failed: %s', error)
    return 1
  print(retained.url)
  return 0


def _publish(source: str, public: bool) -> int:
  visibility: publication.Visibility = 'public' if public else 'private'
  try:
    published = publication.publish_run(source, visibility)
  except (
    credentials.SecretNotFound,
    OSError,
    ValueError,
    publication.PublicationError,
    retention.RetentionError,
  ) as error:
    log.error('benchmark publish failed: %s', error)
    return 1
  print(published.url)
  print(published.record_url)
  return 0


def _import_trails(source: str) -> int:
  try:
    with configured_store() as registry:
      imported = import_trails.import_run(source, registry)
  except import_trails.FAILURES as error:
    log.error('benchmark import-trails failed: %s', error)
    return 1
  for trial in imported:
    print(f'{trial.trial}: {" ".join(trial.trail_ids)}')
  return 0


def _query(sql: Optional[str], sql_file: Optional[Path]) -> int:
  try:
    query.command(sql, sql_file)
  except (credentials.SecretNotFound, OSError, ValueError, query.duckdb.Error) as error:
    log.error('benchmark query failed: %s', error)
    return 1
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(description='build and preserve benchmark runs')
  subparsers = parser.add_subparsers(dest='command')

  bundle_parser = subparsers.add_parser('bundle', help='build the relocatable bro bundle')
  bundle.configure_parser(bundle_parser)

  retain_parser = subparsers.add_parser(
    'retain', help='copy one raw Harbor job to immutable retention storage'
  )
  retain_parser.add_argument('source', help='artifact ref or local Harbor job directory')
  retain_parser.set_handler(_retain)

  publish_parser = subparsers.add_parser(
    'publish', help='publish one retained run to the Harbor Hub'
  )
  publish_parser.add_argument('source', help='retained run prefix')
  visibility = publish_parser.add_mutually_exclusive_group(required=True)
  visibility.add_argument('--public', action='store_true', help='make the Hub job public')
  visibility.add_argument(
    '--private', action='store_false', dest='public', help='keep the Hub job private'
  )
  publish_parser.set_handler(_publish)

  query_parser = subparsers.add_parser(
    'query', help='query retained benchmark manifests with DuckDB'
  )
  source = query_parser.add_mutually_exclusive_group()
  source.add_argument('sql', nargs='?', help='inline SQL statement')
  source.add_argument('--file', dest='sql_file', type=Path, help='SQL file to run')
  query_parser.set_handler(_query)

  import_parser = subparsers.add_parser(
    'import-trails', help="import one retained run's trail stores into the trails registry"
  )
  import_parser.add_argument('source', help='retained run prefix')
  import_parser.set_handler(_import_trails)

  return parser.dispatch(argv)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
