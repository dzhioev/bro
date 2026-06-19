"""tests for the one-time spill-body-attr migration.

A fake low-level DynamoDB (items in AttributeValue form) exercises the scan +
update_item rewrite: only single-key `{'s3': key}` bodies migrate to `body_s3`;
real content is left untouched; a re-run is a no-op.
"""

from boto3.dynamodb.types import TypeSerializer

from trails.server.migrate_spill_body_attr import migrate

_serializer = TypeSerializer()


def _ser(item: dict) -> dict:
  return {k: _serializer.serialize(v) for k, v in item.items()}


class FakeDynamo:
  def __init__(self, items: list[dict]):
    # items in low-level AttributeValue form, mirroring a real scan response.
    self.items = [_ser(it) for it in items]

  def scan(self, **_) -> dict:
    return {'Items': list(self.items)}

  def update_item(self, *, Key, UpdateExpression, ExpressionAttributeValues, **_):
    assert UpdateExpression == 'SET body_s3 = :k REMOVE body'
    for it in self.items:
      if it['trail_id'] == Key['trail_id'] and it['step_id'] == Key['step_id']:
        it['body_s3'] = ExpressionAttributeValues[':k']
        del it['body']
        return
    raise AssertionError('update_item targeted a missing key')


def _step(idx: int, body) -> dict:
  return {'trail_id': 'T1', 'step_id': f'S{idx:03d}', 'kind': 'tool_result', 'body': body}


def test_migrates_only_legacy_spill_bodies():
  dynamo = FakeDynamo(
    [
      _step(0, {'s3': 'trails/T1/steps/S000.json'}),  # legacy spill -> migrate
      _step(1, {'s3': 'x'}),  # genuine content equal to the old sentinel -> migrate too
      _step(2, 'plain text body'),  # real content -> leave
      _step(3, {'s3': 'k', 'url': 'u', 'size': 9}),  # multi-key, not a sentinel -> leave
    ]
  )
  migrated = migrate(dynamo, 'trail_steps', dry_run=False)
  assert migrated == 2
  by_id = {it['step_id']['S']: it for it in dynamo.items}
  assert by_id['S000']['body_s3']['S'] == 'trails/T1/steps/S000.json'
  assert 'body' not in by_id['S000']
  assert by_id['S001']['body_s3']['S'] == 'x'
  assert by_id['S002']['body']['S'] == 'plain text body'
  assert 'body_s3' not in by_id['S003']


def test_dry_run_changes_nothing():
  dynamo = FakeDynamo([_step(0, {'s3': 'k'})])
  migrated = migrate(dynamo, 'trail_steps', dry_run=True)
  assert migrated == 1
  assert 'body_s3' not in dynamo.items[0]
  assert dynamo.items[0]['body']['M'] == {'s3': {'S': 'k'}}


def test_rerun_is_idempotent():
  dynamo = FakeDynamo([_step(0, {'s3': 'k'})])
  migrate(dynamo, 'trail_steps', dry_run=False)
  assert migrate(dynamo, 'trail_steps', dry_run=False) == 0
