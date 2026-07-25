#!/usr/bin/env python
"""Backfill the legacy Claude session-log store into universal trails storage."""

import datetime
import json
import os
import sys
from collections import defaultdict
from typing import Any, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import base.args
from base import log
from trails import session_migration

__cli_name__ = 'trails-migrate-sessions'

DEFAULT_REGION = 'eu-central-1'
DEFAULT_SOURCE_TABLE = 'cw-sessions'
DEFAULT_TARGET_TABLE = 'trails-v2'
DEFAULT_REPORT_PREFIX = 'trails/migrations/session-log-v2/'

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _serialize_item(item: dict) -> dict:
  return {key: _serializer.serialize(value) for key, value in item.items()}


def _deserialize_item(item: dict) -> dict:
  return session_migration.normalise_decimal(
    {key: _deserializer.deserialize(value) for key, value in item.items()}
  )


def _scan(dynamo, table: str) -> list[dict]:
  items: list[dict] = []
  start_key = None
  while True:
    arguments: dict[str, Any] = {'TableName': table}
    if start_key is not None:
      arguments['ExclusiveStartKey'] = start_key
    response = dynamo.scan(**arguments)
    items.extend(_deserialize_item(item) for item in response.get('Items', []))
    start_key = response.get('LastEvaluatedKey')
    if start_key is None:
      return items


def _list_objects(s3, bucket: str) -> list[dict]:
  objects: list[dict] = []
  token = None
  while True:
    arguments: dict[str, Any] = {'Bucket': bucket}
    if token is not None:
      arguments['ContinuationToken'] = token
    response = s3.list_objects_v2(**arguments)
    objects.extend(response.get('Contents', []))
    token = response.get('NextContinuationToken')
    if token is None:
      return objects


def _get_object(s3, bucket: str, key: str) -> bytes:
  return s3.get_object(Bucket=bucket, Key=key)['Body'].read()


def _iso(moment: datetime.datetime) -> str:
  return moment.astimezone(datetime.UTC).isoformat()


def _canonical_source(
  s3,
  bucket: str,
  identity: str,
  objects: list[dict],
  table_item: Optional[dict],
) -> tuple[session_migration.Source, int]:
  candidates = [(metadata, _get_object(s3, bucket, metadata['Key'])) for metadata in objects]
  candidates.sort(key=lambda pair: (len(pair[1]), pair[0]['Key']))
  canonical_metadata, canonical_body = candidates[-1]
  for metadata, body in candidates[:-1]:
    if not canonical_body.startswith(body):
      raise ValueError(
        f'objects for {identity} diverge: {metadata["Key"]} is not a prefix of '
        f'{canonical_metadata["Key"]}'
      )
    log.info(
      'deduplicated %s (%d bytes) into %s (%d bytes)',
      metadata['Key'],
      len(body),
      canonical_metadata['Key'],
      len(canonical_body),
    )
  return (
    session_migration.Source(
      identity=identity,
      key=canonical_metadata['Key'],
      body=canonical_body,
      modified_at=_iso(canonical_metadata['LastModified']),
      table_item=table_item,
      duplicate_keys=tuple(metadata['Key'] for metadata, _ in candidates[:-1]),
    ),
    len(candidates) - 1,
  )


def _inventory(
  dynamo, s3, source_table: str, source_bucket: str
) -> tuple[list[session_migration.Source], dict]:
  rows = _scan(dynamo, source_table)
  log_rows = [row for row in rows if isinstance(row.get('s3_key'), str)]
  non_log_rows = [row for row in rows if not isinstance(row.get('s3_key'), str)]
  for row in non_log_rows:
    log.info('non-log legacy row: %s (%s)', row.get('session_id'), row.get('kind'))

  objects = _list_objects(s3, source_bucket)
  metadata_by_key = {metadata['Key']: metadata for metadata in objects}
  if len(metadata_by_key) != len(objects):
    raise ValueError('legacy bucket inventory contains duplicate keys')
  referenced_keys = {row['s3_key'] for row in log_rows}
  missing = sorted(referenced_keys - set(metadata_by_key))
  if len(missing) > 0:
    raise ValueError(f'{len(missing)} table-referenced legacy objects are missing: {missing[:10]}')

  rows_by_object_identity: dict[str, dict] = {}
  seen_session_ids: set[str] = set()
  for row in log_rows:
    session_id = row['session_id']
    if session_id in seen_session_ids:
      raise ValueError(f'duplicate legacy session id: {session_id}')
    seen_session_ids.add(session_id)
    object_identity = row['s3_key'].rsplit('/', 1)[-1].removesuffix('.jsonl')
    if object_identity in rows_by_object_identity:
      raise ValueError(f'multiple table rows own legacy object identity: {object_identity}')
    rows_by_object_identity[object_identity] = row

  objects_by_identity: dict[str, list[dict]] = defaultdict(list)
  for metadata in objects:
    object_identity = metadata['Key'].rsplit('/', 1)[-1].removesuffix('.jsonl')
    objects_by_identity[object_identity].append(metadata)

  sources: list[session_migration.Source] = []
  deduplicated = 0
  for object_identity, candidates in sorted(objects_by_identity.items()):
    row = rows_by_object_identity.get(object_identity)
    source_identity = row['session_id'] if row is not None else object_identity
    source, duplicate_count = _canonical_source(s3, source_bucket, source_identity, candidates, row)
    sources.append(source)
    deduplicated += duplicate_count

  owned_object_identities = set(rows_by_object_identity)
  orphan_sources = [
    source
    for object_identity, source in zip(sorted(objects_by_identity), sources, strict=True)
    if object_identity not in owned_object_identities
  ]
  orphan_metadata = [metadata for metadata in objects if metadata['Key'] not in referenced_keys]
  report = {
    'table_rows': len(rows),
    'transcript_rows': len(log_rows),
    'non_log_rows': len(non_log_rows),
    'bucket_objects': len(objects),
    'orphan_objects': len(orphan_metadata),
    'orphan_canonical_objects': len(orphan_sources),
    'deduplicated_objects': deduplicated,
    'source_bytes': sum(len(source.body) for source in sources),
  }
  return sources, report


def _existing_object(s3, bucket: str, key: str) -> Optional[bytes]:
  try:
    return _get_object(s3, bucket, key)
  except s3.exceptions.NoSuchKey:
    return None


def _put_object_idempotent(s3, bucket: str, key: str, body: bytes, content_type: str) -> bool:
  existing = _existing_object(s3, bucket, key)
  if existing is not None:
    if existing != body:
      raise ValueError(f'target object differs from migration payload: s3://{bucket}/{key}')
    return False
  s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
  return True


def _put_header_idempotent(dynamo, table: str, header: dict) -> bool:
  try:
    dynamo.put_item(
      TableName=table,
      Item=_serialize_item(header),
      ConditionExpression='attribute_not_exists(id)',
    )
    return True
  except dynamo.exceptions.ConditionalCheckFailedException:
    response = dynamo.get_item(
      TableName=table,
      Key={'id': _serializer.serialize(header['id'])},
      ConsistentRead=True,
    )
    existing = _deserialize_item(response['Item'])
    if existing != header:
      raise ValueError(f'target header differs from migration payload: {header["id"]}')
    return False


def _write_plan(
  dynamo, s3, target_table: str, target_bucket: str, plan: session_migration.SourcePlan
) -> tuple[int, int]:
  headers = 0
  objects = 0
  for trail in plan.trails:
    if _put_object_idempotent(
      s3,
      target_bucket,
      trail.header['native']['s3_key'],
      trail.artifact,
      'application/x-ndjson',
    ):
      objects += 1
    if trail.context is not None and _put_object_idempotent(
      s3,
      target_bucket,
      trail.header['native']['context_s3'],
      trail.context,
      'application/json',
    ):
      objects += 1
    if _put_header_idempotent(dynamo, target_table, trail.header):
      headers += 1
  return headers, objects


def _moment(value: str) -> datetime.datetime:
  return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))


def _resolve_summoners(
  headers: list[dict], bro_headers: list[dict]
) -> tuple[list[tuple[dict, dict]], list[dict]]:
  intervals: dict[str, list[dict]] = defaultdict(list)
  for header in headers:
    location = header.get('location', {})
    workspace = location.get('workspace') if isinstance(location, dict) else None
    if isinstance(workspace, str) and location.get('is_container') is True:
      intervals[workspace].append(header)

  resolved: list[tuple[dict, dict]] = []
  unresolved: list[dict] = []
  for bro_header in bro_headers:
    legacy = bro_header.get('native', {}).get('legacy_summoner')
    marker = legacy.get('session') if isinstance(legacy, dict) else None
    if not isinstance(marker, str) or not marker.startswith('c:'):
      unresolved.append({'trail_id': bro_header['id'], 'legacy_summoner': legacy})
      continue
    workspace = marker.removeprefix('c:')
    child_start = _moment(bro_header['started_at'])
    matches = [
      header
      for header in intervals.get(workspace, [])
      if _moment(header['started_at']) <= child_start <= _moment(header['end']['at'])
    ]
    if len(matches) != 1:
      unresolved.append(
        {
          'trail_id': bro_header['id'],
          'legacy_summoner': legacy,
          'candidate_trail_ids': [header['id'] for header in matches],
        }
      )
      continue
    resolved.append((bro_header, matches[0]))
  return resolved, unresolved


def _write_summoner(dynamo, table: str, bro_header: dict, claude_header: dict) -> None:
  dynamo.update_item(
    TableName=table,
    Key={'id': _serializer.serialize(bro_header['id'])},
    ConditionExpression='attribute_exists(id) AND attribute_exists(native.#legacy)',
    UpdateExpression='SET summoned_by = :summoned_by REMOVE native.#legacy',
    ExpressionAttributeNames={'#legacy': 'legacy_summoner'},
    ExpressionAttributeValues={
      ':summoned_by': _serializer.serialize({'trail_id': claude_header['id']})
    },
  )


def _default_report_key() -> str:
  stamp = datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')
  return f'{DEFAULT_REPORT_PREFIX}{stamp}.json'


def run_migration(
  *,
  dynamo,
  s3,
  source_table: str,
  source_bucket: str,
  target_table: str,
  target_bucket: str,
  dry_run: bool,
) -> dict:
  sources, inventory = _inventory(dynamo, s3, source_table, source_bucket)
  manifests: list[dict] = []
  planned_headers: list[dict] = []
  written_headers = 0
  written_objects = 0
  for index, source in enumerate(sources, start=1):
    plan = session_migration.plan_source(source)
    manifests.append(session_migration.manifest_source(plan))
    planned_headers.extend(trail.header for trail in plan.trails)
    if not dry_run:
      new_headers, new_objects = _write_plan(dynamo, s3, target_table, target_bucket, plan)
      written_headers += new_headers
      written_objects += new_objects
    if index % 50 == 0 or index == len(sources):
      log.info('planned %d/%d canonical legacy artifacts', index, len(sources))

  target_headers = _scan(dynamo, target_table)
  bro_headers = [header for header in target_headers if header.get('harness') == 'bro']
  unresolved_bro_headers = [
    header for header in bro_headers if header.get('native', {}).get('legacy_summoner') is not None
  ]
  newly_resolved, unresolved = _resolve_summoners(planned_headers, unresolved_bro_headers)
  if not dry_run:
    for bro_header, claude_header in newly_resolved:
      _write_summoner(dynamo, target_table, bro_header, claude_header)
  planned_by_id = {header['id']: header for header in planned_headers}
  already_resolved = []
  for header in bro_headers:
    summoned_by = header.get('summoned_by')
    trail_id = summoned_by.get('trail_id') if isinstance(summoned_by, dict) else None
    if isinstance(trail_id, str) and trail_id in planned_by_id:
      already_resolved.append((header, planned_by_id[trail_id]))
  resolved = [*already_resolved, *newly_resolved]

  return {
    'version': 1,
    'source_table': source_table,
    'source_bucket': source_bucket,
    'target_table': target_table,
    'target_bucket': target_bucket,
    'dry_run': dry_run,
    'inventory': inventory,
    'canonical_sources': len(sources),
    'planned_trails': len(planned_headers),
    'planned_artifact_bytes': sum(
      trail['artifact_size_bytes'] for source in manifests for trail in source['trails']
    ),
    'marker_bytes': sum(source['marker_bytes'] for source in manifests),
    'degenerate_sources': sum(1 for source in manifests if source['degenerate']),
    'written_headers': written_headers,
    'written_objects': written_objects,
    'summoners': {
      'legacy': len(resolved) + len(unresolved),
      'resolved': [
        {'bro_trail_id': bro['id'], 'claude_trail_id': claude['id']} for bro, claude in resolved
      ],
      'unresolved': unresolved,
    },
    'sources': manifests,
  }


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='backfill legacy Claude session logs into trails')
  parser.add_argument('--source-table', default=DEFAULT_SOURCE_TABLE)
  parser.add_argument('--target-table', default=DEFAULT_TARGET_TABLE)
  parser.add_argument('--source-bucket', default=None)
  parser.add_argument('--target-bucket', default=None)
  parser.add_argument('--report-key', default=None)
  parser.add_argument('--dry-run', action='store_true')
  parser.add_argument('--aws-region', default=os.environ.get('AWS_REGION', DEFAULT_REGION))
  arguments = parser.parse(argv)
  session = boto3.Session(region_name=arguments['aws_region'])
  account = session.client('sts').get_caller_identity()['Account']
  source_bucket = (
    arguments['source_bucket']
    if arguments['source_bucket'] is not None
    else f'cw-session-logs-{account}'
  )
  target_bucket = (
    arguments['target_bucket'] if arguments['target_bucket'] is not None else f'cw-trails-{account}'
  )
  report = run_migration(
    dynamo=session.client('dynamodb'),
    s3=session.client('s3'),
    source_table=arguments['source_table'],
    source_bucket=source_bucket,
    target_table=arguments['target_table'],
    target_bucket=target_bucket,
    dry_run=arguments['dry_run'],
  )
  payload = json.dumps(report, indent=2, sort_keys=True).encode()
  if not arguments['dry_run']:
    report_key = (
      arguments['report_key'] if arguments['report_key'] is not None else _default_report_key()
    )
    session.client('s3').put_object(
      Bucket=target_bucket,
      Key=report_key,
      Body=payload,
      ContentType='application/json',
    )
    print(f'report: s3://{target_bucket}/{report_key}', file=sys.stderr)
  print(payload.decode())
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
