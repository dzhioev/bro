#!/usr/bin/env python
"""one-time migration: move the spill pointer into a dedicated `body_s3` attr.

Spilled step bodies used to be stored by overloading `body = {'s3': key}`, a
sentinel that collides with a genuine small body equal to `{'s3': ...}`. The
reader now keys off a dedicated sparse `body_s3` attribute; this script rewrites
every legacy row to the new form (`SET body_s3 = :k REMOVE body`).

Run once right after deploying the new storage code. Idempotent — a re-run finds
nothing, since migrated rows no longer carry a single-key `{'s3': key}` body.
"""

import os

import boto3
from boto3.dynamodb.types import TypeDeserializer

import base.args
from base import log

_deserializer = TypeDeserializer()


def _legacy_spill_key(body_attr: dict | None) -> str | None:
  # body_attr is a low-level DynamoDB AttributeValue; a legacy spill body is the
  # single-key map {'s3': <key>}. anything else (real content, missing) is left.
  if body_attr is None:
    return None
  body = _deserializer.deserialize(body_attr)
  if isinstance(body, dict) and list(body.keys()) == ['s3'] and isinstance(body['s3'], str):
    return body['s3']
  return None


def migrate(dynamo, steps_table: str, *, dry_run: bool) -> int:
  scanned = 0
  migrated = 0
  kwargs: dict = {'TableName': steps_table}
  while True:
    response = dynamo.scan(**kwargs)
    for item in response.get('Items', []):
      scanned += 1
      key = _legacy_spill_key(item.get('body'))
      if key is None:
        continue
      migrated += 1
      trail_id = _deserializer.deserialize(item['trail_id'])
      step_id = _deserializer.deserialize(item['step_id'])
      log.info(f'migrating {trail_id}/{step_id} -> body_s3={key}')
      if not dry_run:
        dynamo.update_item(
          TableName=steps_table,
          Key={'trail_id': item['trail_id'], 'step_id': item['step_id']},
          UpdateExpression='SET body_s3 = :k REMOVE body',
          ExpressionAttributeValues={':k': {'S': key}},
        )
    last = response.get('LastEvaluatedKey')
    if last is None:
      break
    kwargs['ExclusiveStartKey'] = last
  log.info(f'scanned {scanned} steps, migrated {migrated}')
  return migrated


def main(argv: list[str]) -> int | None:
  parser = base.args.Parser(description='migrate spilled step bodies to the body_s3 attribute')
  parser.add_argument('--steps-table', default='trail_steps')
  parser.add_argument('--aws-region', default=os.environ.get('AWS_REGION', 'eu-central-1'))
  parser.add_argument('--dry-run', action='store_true')
  args = parser.parse(argv)

  session = boto3.Session(region_name=args['aws_region'])
  migrate(session.client('dynamodb'), args['steps_table'], dry_run=args['dry_run'])
