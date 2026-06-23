"""storage-layer list/pagination tests.

`server_test.FakeStorage` fakes cursors at a high level — a bare `trail_id` for
every path — so it can neither reproduce the GSI cursor round-trip nor the
ordering the real index provides. These tests run the real `Storage` against a
fake DynamoDB that emits correctly-shaped `LastEvaluatedKey`s: every list path is
a GSI query (`bro` / `parent` / the constant-PK `all` index), and the LEK is the
triple `{trail_id, <index PK>, started_at}` the cursor round-trip must survive.
"""

import io
import json
from typing import Optional

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from trails.server.storage import (
  GSI_PK_ATTR,
  GSI_PK_VALUE,
  SPILLOVER_THRESHOLD_BYTES,
  Storage,
  TrailNotFound,
)

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

# IndexName -> (index PK attr, index SK attr); mirrors the GSIs storage queries.
_INDEXES = {
  'bro-started_at-index': ('bro', 'started_at'),
  'parent-trail-id-index': ('parent_trail_id', 'started_at'),
  'all-index': (GSI_PK_ATTR, 'started_at'),
}


def _ser(item: dict) -> dict:
  return {k: _serializer.serialize(v) for k, v in item.items()}


def _des(item: dict) -> dict:
  return {k: _deserializer.deserialize(v) for k, v in item.items()}


class FakeDynamo:
  """minimal DynamoDB stand-in faithful to the contract `Storage` depends on:
  paged `query` with `ExclusiveStartKey` and a `LastEvaluatedKey` whose shape
  matches the real service (base PK + index PK/SK for a GSI query). Items are
  stored deserialized and (de)serialized at the boundary.
  """

  def __init__(self, items: list[dict]):
    self._items = list(items)

  def query(self, **kwargs) -> dict:
    pk_attr, sk_attr = _INDEXES[kwargs['IndexName']]
    values = {
      k: _deserializer.deserialize(v) for k, v in kwargs['ExpressionAttributeValues'].items()
    }
    matched = [it for it in self._items if it.get(pk_attr) == values[':pk']]
    if ':lo' in values:
      matched = [it for it in matched if it[sk_attr] >= values[':lo']]
    if ':hi' in values:
      matched = [it for it in matched if it[sk_attr] <= values[':hi']]
    # storage passes ScanIndexForward=False -> descending on the SK.
    forward = kwargs.get('ScanIndexForward', True)
    ordered = sorted(matched, key=lambda it: it[sk_attr], reverse=not forward)
    return self._page(ordered, kwargs, key_attrs=['trail_id', pk_attr, sk_attr])

  def _page(self, ordered: list[dict], kwargs: dict, *, key_attrs: list[str]) -> dict:
    start = 0
    start_key = kwargs.get('ExclusiveStartKey')
    if start_key is not None:
      last_id = _des(start_key)['trail_id']
      start = next(i for i, it in enumerate(ordered) if it['trail_id'] == last_id) + 1
    limit = kwargs['Limit']
    page = ordered[start : start + limit]
    response: dict = {'Items': [_ser(it) for it in page]}
    if start + limit < len(ordered):
      last = page[-1]
      response['LastEvaluatedKey'] = _ser({attr: last[attr] for attr in key_attrs})
    return response


def _trail(idx: int, *, bro: str, parent: Optional[str], indexed: bool = True) -> dict:
  item = {
    'trail_id': f'trail-{idx:03d}',
    'bro': bro,
    'started_at': f'2026-06-07T00:00:{idx:02d}.000000Z',
  }
  if indexed:
    item[GSI_PK_ATTR] = GSI_PK_VALUE
  if parent is not None:
    item['parent_trail_id'] = parent
  return item


# 5 'dev' trails (3 of them forks of P1) + 1 unrelated trail under a different bro.
_TRAILS = [
  _trail(0, bro='dev', parent=None),
  _trail(1, bro='dev', parent='P1'),
  _trail(2, bro='dev', parent=None),
  _trail(3, bro='dev', parent='P1'),
  _trail(4, bro='dev', parent='P1'),
  _trail(5, bro='other', parent=None),
]


def _store(items: Optional[list[dict]] = None) -> Storage:
  return Storage(
    dynamo=FakeDynamo(_TRAILS if items is None else items),
    s3=None,
    trails_table='trails',
    steps_table='trail_steps',
    bucket='bucket',
  )


async def _collect(
  store: Storage, *, bro=None, parent=None, since=None, until=None, limit: int
) -> list[dict]:
  """paginate to exhaustion, returning the gathered trails — mirrors
  `TrailsClient.iter_trails`.
  """
  trails: list[dict] = []
  cursor: Optional[str] = None
  while True:
    page = await store.list_trails(
      bro=bro, parent=parent, since=since, until=until, cursor=cursor, limit=limit
    )
    trails.extend(page['trails'])
    cursor = page['next']
    if cursor is None:
      break
  return trails


def _ids(trails: list[dict]) -> list[str]:
  return [t['trail_id'] for t in trails]


@pytest.mark.asyncio
async def test_all_index_newest_first():
  store = _store()
  single = await _collect(store, limit=100)
  # global list: every bro, newest started_at first, nothing dropped.
  assert _ids(single)[0] == 'trail-005'  # the most recent, regardless of bro
  started = [t['started_at'] for t in single]
  assert started == sorted(started, reverse=True)
  assert len(single) == len(_TRAILS)


@pytest.mark.asyncio
async def test_all_index_pagination_round_trips():
  store = _store()
  single = await _collect(store, limit=100)
  paged = await _collect(store, limit=2)
  # paging must neither drop, duplicate, nor reorder relative to one big page.
  assert _ids(paged) == _ids(single)


@pytest.mark.asyncio
async def test_all_index_since_until_between():
  store = _store()
  window = await _collect(
    store,
    since='2026-06-07T00:00:01.000000Z',
    until='2026-06-07T00:00:03.000000Z',
    limit=2,
  )
  # BETWEEN bounds are inclusive; result stays newest-first within the window.
  assert _ids(window) == ['trail-003', 'trail-002', 'trail-001']


@pytest.mark.asyncio
async def test_all_index_skips_rows_without_gsi_pk():
  # rows created before the gsi_pk stamp (and not yet backfilled) lack the
  # constant attribute, so the sparse all-index omits them.
  store = _store(
    [
      _trail(0, bro='dev', parent=None, indexed=False),
      _trail(1, bro='dev', parent=None),
    ]
  )
  assert _ids(await _collect(store, limit=100)) == ['trail-001']


@pytest.mark.asyncio
async def test_bro_pagination_round_trips():
  store = _store()
  single = await _collect(store, bro='dev', limit=100)
  paged = await _collect(store, bro='dev', limit=2)
  assert _ids(paged) == _ids(single)
  # newest first, only the 'dev' trails.
  assert _ids(single) == ['trail-004', 'trail-003', 'trail-002', 'trail-001', 'trail-000']


@pytest.mark.asyncio
async def test_parent_pagination_round_trips():
  store = _store()
  single = await _collect(store, parent='P1', limit=100)
  paged = await _collect(store, parent='P1', limit=2)
  assert _ids(paged) == _ids(single)
  assert _ids(single) == ['trail-004', 'trail-003', 'trail-001']


@pytest.mark.asyncio
async def test_gsi_cursor_is_a_json_object():
  # the regression: the GSI decode path does json.loads(cursor), so the encode
  # side must emit the full LEK triple as a JSON object — not a bare ULID.
  store = _store()
  page = await store.list_trails(
    bro='dev', parent=None, since=None, until=None, cursor=None, limit=2
  )
  decoded = json.loads(page['next'])
  assert set(decoded) == {'trail_id', 'bro', 'started_at'}


@pytest.mark.asyncio
async def test_all_index_cursor_is_a_json_object():
  store = _store()
  page = await store.list_trails(
    bro=None, parent=None, since=None, until=None, cursor=None, limit=2
  )
  decoded = json.loads(page['next'])
  # the all-index LEK is the same triple shape, keyed on the constant gsi_pk.
  assert set(decoded) == {'trail_id', GSI_PK_ATTR, 'started_at'}
  assert decoded['trail_id'] == 'trail-004'


# spill round-trip: the spill pointer lives in body_s3, so a body that equals the
# old {'s3': key} sentinel must survive as literal content (the regression), and a
# genuinely spilled body must resolve back through body_s3.


class _FakeS3:
  def __init__(self):
    self.objects: dict[str, bytes] = {}

  def put_object(self, *, Key, Body, **_):
    self.objects[Key] = Body

  def head_object(self, *, Key, **_):
    return {'ContentLength': len(self.objects[Key])}

  def get_object(self, *, Key, **_):
    return {'Body': io.BytesIO(self.objects[Key])}


class _FakeStepsDynamo:
  """faithful enough to exercise the per-step transaction's two conditions: the
  step Put's `attribute_not_exists(step_id)` (idempotency) and the trail Update's
  `attribute_exists(trail_id)` (trail-not-found). On a failed condition it raises
  `TransactionCanceledException` with positional `CancellationReasons` matching
  the real service — item 0 the step Put, item 1 the trail Update.

  `existing_trails=None` means "every trail exists" (permissive — what the spill
  round-trip tests want); pass an explicit set to make the Update condition bite.
  """

  class exceptions:
    class TransactionCanceledException(Exception):
      pass

  def __init__(self, existing_trails: Optional[set[str]] = None):
    self._steps: list[dict] = []
    self._existing_trails = existing_trails

  def transact_write_items(self, *, TransactItems):
    reasons: list[dict] = []
    staged: list[dict] = []
    failed = False
    for ti in TransactItems:
      put = ti.get('Put')
      update = ti.get('Update')
      if put is not None and put['TableName'] == 'trail_steps':
        item = _des(put['Item'])
        duplicate = put.get('ConditionExpression') == 'attribute_not_exists(step_id)' and any(
          s['trail_id'] == item['trail_id'] and s['step_id'] == item['step_id'] for s in self._steps
        )
        if duplicate:
          reasons.append({'Code': 'ConditionalCheckFailed'})
          failed = True
        else:
          reasons.append({'Code': 'None'})
          staged.append(item)
      elif update is not None:
        tid = _des(update['Key'])['trail_id']
        missing = (
          update.get('ConditionExpression') == 'attribute_exists(trail_id)'
          and self._existing_trails is not None
          and tid not in self._existing_trails
        )
        if missing:
          reasons.append({'Code': 'ConditionalCheckFailed'})
          failed = True
        else:
          reasons.append({'Code': 'None'})
      else:
        reasons.append({'Code': 'None'})
    if failed:
      exc = self.exceptions.TransactionCanceledException('cancelled')
      exc.response = {'CancellationReasons': reasons}  # type: ignore[attr-defined]
      raise exc
    self._steps.extend(staged)

  def query(self, **kwargs):
    tid = _deserializer.deserialize(kwargs['ExpressionAttributeValues'][':tid'])
    items = [it for it in self._steps if it['trail_id'] == tid]
    return {'Items': [_ser(it) for it in items]}


def _spill_store(
  existing_trails: Optional[set[str]] = None,
) -> tuple[Storage, _FakeStepsDynamo, _FakeS3]:
  dynamo = _FakeStepsDynamo(existing_trails)
  s3 = _FakeS3()
  store = Storage(
    dynamo=dynamo, s3=s3, trails_table='trails', steps_table='trail_steps', bucket='bucket'
  )
  return store, dynamo, s3


@pytest.mark.asyncio
async def test_literal_s3_body_round_trips_as_content():
  store, dynamo, s3 = _spill_store()
  await store.put_step(trail_id='T1', kind='tool_result', body={'s3': 'x'}, extras={})
  # small body stays inline: no spill, no S3 object, body untouched.
  assert len(s3.objects) == 0
  assert 'body_s3' not in dynamo._steps[0]
  page = await store.query_steps('T1', after=None, limit=100)
  step = page['steps'][0]
  assert step['body'] == {'s3': 'x'}
  assert 'body_s3' not in step


@pytest.mark.asyncio
async def test_spilled_body_resolves_via_body_s3():
  store, dynamo, s3 = _spill_store()
  big = {'big': 'x' * (SPILLOVER_THRESHOLD_BYTES + 1)}
  await store.put_step(trail_id='T1', kind='llm_call', body=big, extras={})
  # spilled: pointer is in body_s3, real content omitted from the row.
  stored = dynamo._steps[0]
  assert 'body' not in stored
  assert stored['body_s3'] in s3.objects
  page = await store.query_steps('T1', after=None, limit=100)
  step = page['steps'][0]
  # under the 1MB inline cap, so it comes back as content, and the helper attr
  # never leaks into the row.
  assert step['body'] == big
  assert 'body_s3' not in step


# idempotency: a retried POST reuses the client-minted step_id, so the conditional
# step Put cancels the whole transaction (step + aggregate increment) atomically.


@pytest.mark.asyncio
async def test_retried_step_id_is_idempotent():
  store, dynamo, _ = _spill_store()
  first = await store.put_step(trail_id='T1', kind='llm_call', body='x', extras={}, step_id='S1')
  second = await store.put_step(trail_id='T1', kind='llm_call', body='x', extras={}, step_id='S1')
  # the retry writes no second row (so the aggregate increment never re-ran) and
  # reports success rather than raising.
  assert len([s for s in dynamo._steps if s['step_id'] == 'S1']) == 1
  assert first['step_id'] == 'S1'
  assert second.get('duplicate') is True


@pytest.mark.asyncio
async def test_distinct_step_ids_both_written():
  store, dynamo, _ = _spill_store()
  await store.put_step(trail_id='T1', kind='reasoning', body='a', extras={}, step_id='S1')
  await store.put_step(trail_id='T1', kind='reasoning', body='b', extras={}, step_id='S2')
  assert {s['step_id'] for s in dynamo._steps} == {'S1', 'S2'}


@pytest.mark.asyncio
async def test_put_step_on_missing_trail_raises_trail_not_found():
  # empty existing-trails set -> the trail Update's attribute_exists condition
  # fails (item 1), which must surface as TrailNotFound, not a duplicate.
  store, _, _ = _spill_store(existing_trails=set())
  with pytest.raises(TrailNotFound):
    await store.put_step(trail_id='ghost', kind='reasoning', body='x', extras={}, step_id='S1')


@pytest.mark.asyncio
async def test_server_minted_step_id_when_client_omits():
  store, dynamo, _ = _spill_store()
  result = await store.put_step(trail_id='T1', kind='reasoning', body='x', extras={})
  # older clients send no step_id; the server mints one and the row still lands.
  assert len(result['step_id']) > 0
  assert dynamo._steps[0]['step_id'] == result['step_id']


@pytest.mark.asyncio
async def test_retried_end_is_idempotent():
  store, dynamo, _ = _spill_store()
  await store.end_trail(trail_id='T1', reason='terminal', continuation=None, step_id='E1')
  second = await store.end_trail(trail_id='T1', reason='terminal', continuation=None, step_id='E1')
  assert len([s for s in dynamo._steps if s['step_id'] == 'E1']) == 1
  assert second.get('duplicate') is True
