#!/usr/bin/env python
"""`benchmark` — post-run benchmark workflows."""

import sys
from typing import Optional

import bro.base.args as base_args
from bro import artifact
from bro.base import credentials, log
from bro.benchmark import bundle, retention

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

  return parser.dispatch(argv)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
