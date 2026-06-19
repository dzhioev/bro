#!/usr/bin/env python
"""one-time backfill: stamp the constant `gsi_pk` on pre-existing trail rows.

The `all-index` GSI (global newest-first `trails list`) keys on the
constant `gsi_pk` attribute that `storage.create_trail` writes on every new
trail. Rows created before that change lack the attribute and so are absent from
the sparse index — this scans the trails table and sets `gsi_pk` on any row
missing it, making the bare-list path complete.

Idempotent: the scan filters to rows missing the attribute and the update is
conditioned on its absence, so re-runs and rows the live server already stamped
are skipped. `--dry-run` logs the same lines but issues no writes.

Run once after deploying the GSI, with AWS credentials for the trails account
(e.g. from a `cw ss --grant aws` session): `trails.server.backfill-gsi-pk`.
"""

import os
import sys

import boto3

import base.args
from base import log
from trails.server.storage import GSI_PK_ATTR, GSI_PK_VALUE


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='backfill the constant gsi_pk on existing trail rows')
  parser.add_argument('--trails-table', default=os.environ.get('TRAILS_TABLE', 'trails'))
  parser.add_argument('--aws-region', default=os.environ.get('AWS_REGION', 'eu-central-1'))
  parser.add_argument('--dry-run', action='store_true')
  args = parser.parse(argv)

  dynamo = boto3.Session(region_name=args['aws_region']).client('dynamodb')
  table = args['trails_table']
  dry_run = args['dry_run']

  stamped = 0
  start_key = None
  while True:
    scan_kwargs: dict = {
      'TableName': table,
      'ProjectionExpression': 'trail_id',
      'FilterExpression': 'attribute_not_exists(#g)',
      'ExpressionAttributeNames': {'#g': GSI_PK_ATTR},
    }
    if start_key is not None:
      scan_kwargs['ExclusiveStartKey'] = start_key
    response = dynamo.scan(**scan_kwargs)

    for item in response.get('Items', []):
      trail_id = item['trail_id']['S']
      log.info('stamping %s=%s on %s', GSI_PK_ATTR, GSI_PK_VALUE, trail_id)
      if not dry_run:
        try:
          dynamo.update_item(
            TableName=table,
            Key={'trail_id': {'S': trail_id}},
            UpdateExpression='SET #g = :v',
            ConditionExpression='attribute_not_exists(#g)',
            ExpressionAttributeNames={'#g': GSI_PK_ATTR},
            ExpressionAttributeValues={':v': {'S': GSI_PK_VALUE}},
          )
        except dynamo.exceptions.ConditionalCheckFailedException:
          # the live server stamped it between the scan and this update; skip.
          log.info('skip %s: already stamped', trail_id)
          continue
      stamped += 1

    start_key = response.get('LastEvaluatedKey')
    if start_key is None:
      break

  suffix = ' (dry run)' if dry_run else ''
  log.info('backfill complete%s: %d rows stamped with %s', suffix, stamped, GSI_PK_ATTR)
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
