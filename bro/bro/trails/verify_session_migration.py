#!/usr/bin/env python
"""Verify a legacy session-log migration manifest against both stores."""

import datetime
import json
import os
import sys
from typing import Any, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import base.args
from trails import session_migration
from trails.client import default_client
from trails.migrate_sessions import _get_object, _inventory, _scan

__cli_name__ = 'trails-verify-session-migration'

DEFAULT_VERIFICATION_PREFIX = 'trails/migrations/session-log-v2-verification/'

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _get_header(dynamo, table: str, trail_id: str) -> dict:
  response = dynamo.get_item(
    TableName=table,
    Key={'id': _serializer.serialize(trail_id)},
    ConsistentRead=True,
  )
  raw = response.get('Item')
  if raw is None:
    raise ValueError(f'target trail header is missing: {trail_id}')
  return session_migration.normalise_decimal(
    {key: _deserializer.deserialize(value) for key, value in raw.items()}
  )


def _assert_equal(actual: Any, expected: Any, message: str) -> None:
  if actual != expected:
    raise ValueError(f'{message}: expected {expected!r}, got {actual!r}')


def _verify_target_source(dynamo, s3, target_table: str, target_bucket: str, source: dict) -> None:
  for trail in source['trails']:
    header = _get_header(dynamo, target_table, trail['id'])
    _assert_equal(header, trail['header'], f'header mismatch for {trail["id"]}')
    artifact = _get_object(s3, target_bucket, header['native']['s3_key'])
    _assert_equal(len(artifact), trail['artifact_size_bytes'], f'artifact size for {trail["id"]}')
    _assert_equal(
      session_migration.sha256(artifact),
      trail['artifact_sha256'],
      f'artifact hash for {trail["id"]}',
    )
    _assert_equal(
      len(artifact.splitlines()), header['native']['line_count'], f'line count for {trail["id"]}'
    )
    _assert_equal(len(artifact), header['native']['size_bytes'], f'byte count for {trail["id"]}')
    scan = session_migration.Scan.from_lines(artifact.splitlines(keepends=True))
    _assert_equal(scan.usage, header['native']['usage'], f'usage totals for {trail["id"]}')
    context_hash = trail.get('context_sha256')
    context_key = header['native'].get('context_s3')
    if context_hash is None:
      _assert_equal(context_key, None, f'unexpected launch context for {trail["id"]}')
    else:
      context = _get_object(s3, target_bucket, context_key)
      _assert_equal(
        session_migration.sha256(context), context_hash, f'launch context for {trail["id"]}'
      )
  native_bytes = sum(trail['artifact_size_bytes'] for trail in source['trails'])
  _assert_equal(
    native_bytes + source['marker_bytes'],
    source['source_size_bytes'],
    f'source byte coverage for {source["source_key"]}',
  )


def _expected_render_chain(source: dict) -> list[str]:
  expected: list[str] = []
  for trail in source['trails']:
    if 'forked_from' not in trail['header']:
      expected = []
    expected.append(trail['id'])
  return expected


def _verify_service_resolution(sources: list[dict]) -> int:
  checks = 0
  with default_client() as client:
    for source in sources:
      resolution_id = source['trails'][0]['id'] if source['orphan'] else source['identity']
      header = client.get_trail(resolution_id)
      _assert_equal(header['id'], resolution_id, f'service id resolution for {resolution_id}')
      target = source['trails'][-1]
      expected_chain = _expected_render_chain(source)
      actual_chain = [target['id']]
      current = client.get_trail(target['id'])
      seen = {current['id']}
      while current.get('forked_from') is not None:
        current = client.get_trail(current['forked_from']['trail_id'])
        if current['id'] in seen:
          raise ValueError(f'fork cycle while rendering {target["id"]}')
        seen.add(current['id'])
        actual_chain.append(current['id'])
      actual_chain.reverse()
      _assert_equal(actual_chain, expected_chain, f'fork rendering chain for {target["id"]}')
      checks += 1
  return checks


def _verify_summoners(dynamo, target_table: str, summoners: dict) -> None:
  for resolution in summoners['resolved']:
    header = _get_header(dynamo, target_table, resolution['bro_trail_id'])
    _assert_equal(
      header.get('summoned_by'),
      {'trail_id': resolution['claude_trail_id']},
      f'resolved summoner for {resolution["bro_trail_id"]}',
    )
    if header.get('native', {}).get('legacy_summoner') is not None:
      raise ValueError(f'resolved trail retains legacy_summoner: {resolution["bro_trail_id"]}')
  for unresolved in summoners['unresolved']:
    header = _get_header(dynamo, target_table, unresolved['trail_id'])
    _assert_equal(
      header.get('native', {}).get('legacy_summoner'),
      unresolved['legacy_summoner'],
      f'unresolved summoner for {unresolved["trail_id"]}',
    )
    if header.get('summoned_by') is not None:
      raise ValueError(f'unresolved trail has a universal summoned_by: {unresolved["trail_id"]}')


def verify(
  *,
  dynamo,
  s3,
  report: dict,
  service_checks: bool,
) -> dict:
  if report.get('dry_run') is not False:
    raise ValueError('verification requires a report from a non-dry-run migration')
  sources, inventory = _inventory(dynamo, s3, report['source_table'], report['source_bucket'])
  _assert_equal(inventory, report['inventory'], 'live legacy inventory')
  _assert_equal(len(sources), len(report['sources']), 'canonical source count')
  report_by_key = {source['source_key']: source for source in report['sources']}
  if len(report_by_key) != len(report['sources']):
    raise ValueError('migration report repeats a canonical source key')

  expected_ids: set[str] = set()
  for source in sources:
    planned = session_migration.manifest_source(session_migration.plan_source(source))
    recorded = report_by_key.get(source.key)
    if recorded is None:
      raise ValueError(f'canonical source is absent from migration report: {source.key}')
    _assert_equal(planned, recorded, f'migration plan drift for {source.key}')
    _verify_target_source(dynamo, s3, report['target_table'], report['target_bucket'], recorded)
    expected_ids.update(trail['id'] for trail in recorded['trails'])

  legacy_headers = {
    header['id']
    for header in _scan(dynamo, report['target_table'])
    if header.get('harness') == 'claude'
    and header.get('version') == session_migration.LEGACY_VERSION
  }
  _assert_equal(legacy_headers, expected_ids, 'target legacy Claude header ids')
  _verify_summoners(dynamo, report['target_table'], report['summoners'])
  service_count = _verify_service_resolution(report['sources']) if service_checks else 0
  return {
    'verified_at': datetime.datetime.now(datetime.UTC).isoformat(),
    'migration_sources': len(report['sources']),
    'migration_trails': len(expected_ids),
    'source_bytes': report['inventory']['source_bytes'],
    'artifact_bytes': report['planned_artifact_bytes'],
    'marker_bytes': report['marker_bytes'],
    'summoners_resolved': len(report['summoners']['resolved']),
    'summoners_unresolved': len(report['summoners']['unresolved']),
    'service_resolution_checks': service_count,
    'status': 'ok',
  }


def _default_verification_key() -> str:
  stamp = datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')
  return f'{DEFAULT_VERIFICATION_PREFIX}{stamp}.json'


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='verify a legacy session-log migration report')
  parser.add_argument('--report-key', required=True)
  parser.add_argument('--target-bucket', default=None)
  parser.add_argument('--verification-key', default=None)
  parser.add_argument('--skip-service-checks', action='store_true')
  parser.add_argument('--aws-region', default=os.environ.get('AWS_REGION', 'eu-central-1'))
  arguments = parser.parse(argv)
  session = boto3.Session(region_name=arguments['aws_region'])
  account = session.client('sts').get_caller_identity()['Account']
  target_bucket = (
    arguments['target_bucket'] if arguments['target_bucket'] is not None else f'cw-trails-{account}'
  )
  s3 = session.client('s3')
  report = json.loads(_get_object(s3, target_bucket, arguments['report_key']))
  result = verify(
    dynamo=session.client('dynamodb'),
    s3=s3,
    report=report,
    service_checks=not arguments['skip_service_checks'],
  )
  payload = json.dumps(result, indent=2, sort_keys=True).encode()
  verification_key = (
    arguments['verification_key']
    if arguments['verification_key'] is not None
    else _default_verification_key()
  )
  s3.put_object(
    Bucket=target_bucket,
    Key=verification_key,
    Body=payload,
    ContentType='application/json',
  )
  print(payload.decode())
  print(f'verification: s3://{target_bucket}/{verification_key}', file=sys.stderr)
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
