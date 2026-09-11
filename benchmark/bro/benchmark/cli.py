#!/usr/bin/env python
"""`benchmark` — post-run benchmark workflows."""

import sys
from typing import Optional

import bro.base.args as base_args
from bro import artifact
from bro.base import credentials, log
from bro.benchmark import bundle, publication, retention

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
  ) as error:
    log.error('benchmark publish failed: %s', error)
    return 1
  print(published.url)
  print(published.record_url)
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

  return parser.dispatch(argv)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
