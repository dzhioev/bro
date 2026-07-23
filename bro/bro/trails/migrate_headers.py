#!/usr/bin/env python
"""Idempotently copy legacy bro headers into the universal trails table."""

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import base.args
from trails.server import storage_types
from trails.server.backends import add_numeric_maps

__cli_name__ = 'trails-migrate-headers'

DEFAULT_SOURCE_TABLE = 'trails'
DEFAULT_TARGET_TABLE = 'trails-v2'
DEFAULT_STEPS_TABLE = 'trail_steps'
DEFAULT_REGION = 'eu-central-1'

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _serialize(value: Any) -> dict:
  return _serializer.serialize(value)


def _serialize_item(item: dict) -> dict:
  return {key: _serialize(value) for key, value in item.items()}


def _deserialize_item(item: dict) -> dict:
  return storage_types.normalise_decimal(
    {key: _deserializer.deserialize(value) for key, value in item.items()}
  )


def _surface(value: str) -> str:
  return {'cli:bro_run': 'ask', 'fork': 'call'}.get(value, value)


def _end(header: dict, steps: list[dict]) -> Optional[dict]:
  ended_at = header.get('ended_at')
  reason = header.get('end_reason')
  if ended_at is None or reason is None:
    return None
  mapped_reason = 'ok' if reason == 'terminal' else reason
  end: dict[str, str] = {'at': ended_at, 'reason': mapped_reason}
  for step in reversed(steps):
    body = step.get('body')
    if step.get('kind') == 'end' and isinstance(body, dict) and isinstance(body.get('detail'), str):
      end['detail'] = body['detail']
      break
    if mapped_reason == 'error' and step.get('kind') == 'error' and isinstance(body, dict):
      message = body.get('message')
      if isinstance(message, str):
        end['detail'] = message
        break
  return end


def _usage(steps: list[dict]) -> dict:
  totals: dict[str, dict] = {}
  for step in steps:
    if step.get('kind') != 'llm_call':
      continue
    body = step.get('body')
    response = body.get('response') if isinstance(body, dict) else None
    if not isinstance(response, dict):
      raise ValueError(f'llm_call {step.get("step_id")} has no response object')
    model = response.get('model')
    usage = response.get('usage')
    if not isinstance(model, str) or not isinstance(usage, dict):
      raise ValueError(f'llm_call {step.get("step_id")} has no model/usage counters')
    totals[model] = add_numeric_maps(totals.get(model, {}), usage)
  return totals


def migrate_header(header: dict, steps: list[dict]) -> dict:
  aggregates = header.get('aggregates')
  aggregates = aggregates if isinstance(aggregates, dict) else {}
  counts = aggregates.get('step_counts_by_kind')
  counts = counts if isinstance(counts, dict) else {}
  native: dict[str, Any] = {
    'llm': header['llm_spec'],
    'step_counts_by_kind': counts,
    'usage': _usage(steps),
  }
  old_summoner = header.get('summoner')
  summoned_by = None
  if isinstance(old_summoner, dict) and isinstance(old_summoner.get('trail_id'), str):
    summoned_by = {'trail_id': old_summoner['trail_id']}
  elif old_summoner is not None:
    native['legacy_summoner'] = old_summoner

  item: dict[str, Any] = {
    'id': header['trail_id'],
    'harness': 'bro',
    'bro': header['bro'],
    'version': '1',
    'started_at': header['started_at'],
    'end': _end(header, steps),
    'last_alive_at': header.get('last_alive_at', header['started_at']),
    'interactive': header['interactive'],
    'surface': _surface(header['entry_point']),
    'turn_count': int(counts.get('user_input', 0)),
    'native': native,
    'gsi_pk': 'trail',
  }
  parent = header.get('parent')
  if isinstance(parent, dict) and parent.get('relationship') in (None, 'fork'):
    item['forked_from'] = {'trail_id': parent['trail_id'], 'step_id': parent['step_id']}
    item['forked_from_id'] = parent['trail_id']
  if summoned_by is not None:
    item['summoned_by'] = summoned_by
  for optional in ('subject', 'hold', 'location'):
    if header.get(optional) is not None:
      item[optional] = header[optional]
  return item


class Migration:
  def __init__(
    self,
    *,
    dynamo,
    s3,
    source_table: str,
    target_table: str,
    steps_table: str,
    bucket: str,
    dry_run: bool,
  ):
    self._dynamo = dynamo
    self._s3 = s3
    self._source_table = source_table
    self._target_table = target_table
    self._steps_table = steps_table
    self._bucket = bucket
    self._dry_run = dry_run

  def run(self) -> dict:
    report: dict[str, Any] = {
      'source_table': self._source_table,
      'target_table': self._target_table,
      'dry_run': self._dry_run,
      'headers': 0,
      'steps': 0,
      'usage_models': 0,
      'unresolved_summoners': 0,
    }
    start_key = None
    while True:
      arguments: dict[str, Any] = {'TableName': self._source_table}
      if start_key is not None:
        arguments['ExclusiveStartKey'] = start_key
      response = self._dynamo.scan(**arguments)
      for raw_header in response.get('Items', []):
        header = _deserialize_item(raw_header)
        steps = self._steps(header['trail_id'])
        item = migrate_header(header, steps)
        report['headers'] += 1
        report['steps'] += len(steps)
        report['usage_models'] += len(item['native']['usage'])
        if 'legacy_summoner' in item['native']:
          report['unresolved_summoners'] += 1
        if not self._dry_run:
          self._dynamo.put_item(TableName=self._target_table, Item=_serialize_item(item))
      start_key = response.get('LastEvaluatedKey')
      if start_key is None:
        return report

  def _steps(self, trail_id: str) -> list[dict]:
    steps: list[dict] = []
    start_key = None
    while True:
      arguments: dict[str, Any] = {
        'TableName': self._steps_table,
        'KeyConditionExpression': 'trail_id = :trail_id',
        'ExpressionAttributeValues': {':trail_id': _serialize(trail_id)},
      }
      if start_key is not None:
        arguments['ExclusiveStartKey'] = start_key
      response = self._dynamo.query(**arguments)
      for raw_step in response.get('Items', []):
        step = _deserialize_item(raw_step)
        body_s3 = step.pop('body_s3', None)
        if body_s3 is not None:
          stored = self._s3.get_object(Bucket=self._bucket, Key=body_s3)
          payload = stored['Body'].read()
          try:
            step['body'] = json.loads(payload)
          except json.JSONDecodeError:
            step['body'] = payload.decode('utf-8')
        steps.append(step)
      start_key = response.get('LastEvaluatedKey')
      if start_key is None:
        return steps


def _default_report_key() -> str:
  stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
  return f'trails/migrations/bro-header-v2/{stamp}.json'


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='copy legacy bro headers into the universal table')
  parser.add_argument('--source-table', default=DEFAULT_SOURCE_TABLE)
  parser.add_argument('--target-table', default=DEFAULT_TARGET_TABLE)
  parser.add_argument('--steps-table', default=DEFAULT_STEPS_TABLE)
  parser.add_argument('--bucket', required=True)
  parser.add_argument('--report-key', default=None)
  parser.add_argument('--dry-run', action='store_true')
  parser.add_argument('--aws-region', default=os.environ.get('AWS_REGION', DEFAULT_REGION))
  args = parser.parse(argv)
  session = boto3.Session(region_name=args['aws_region'])
  migration = Migration(
    dynamo=session.client('dynamodb'),
    s3=session.client('s3'),
    source_table=args['source_table'],
    target_table=args['target_table'],
    steps_table=args['steps_table'],
    bucket=args['bucket'],
    dry_run=args['dry_run'],
  )
  report = migration.run()
  report_key = args['report_key'] if args['report_key'] is not None else _default_report_key()
  payload = json.dumps(report, indent=2, sort_keys=True).encode('utf-8')
  if not args['dry_run']:
    session.client('s3').put_object(
      Bucket=args['bucket'], Key=report_key, Body=payload, ContentType='application/json'
    )
  print(payload.decode('utf-8'))
  if not args['dry_run']:
    print(f'report: s3://{args["bucket"]}/{report_key}', file=sys.stderr)
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
