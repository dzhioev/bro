"""storage-layer pagination tests.

`server_test.FakeStorage` fakes cursors at a high level — bare `trail_id` for
every path — so it cannot reproduce the encode/decode mismatch that broke
`--bro` / `--parent` pagination on page 2. These tests run the real `Storage`
against a fake DynamoDB that emits correctly-shaped `LastEvaluatedKey`s: the
base-table scan returns `{trail_id}`, a GSI query returns the triple
`{trail_id, <index PK>, started_at}`. That LEK shape is exactly what the cursor
round-trip has to survive.
"""

import json

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from trails.server.storage import Storage

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

# IndexName -> (index PK attr, index SK attr); mirrors the GSIs storage queries.
_INDEXES = {
  'bro-started_at-index': ('bro', 'started_at'),
  'parent-trail-id-index': ('parent_trail_id', 'started_at'),
}


def _ser(item: dict) -> dict:
  return {k: _serializer.serialize(v) for k, v in item.items()}


def _des(item: dict) -> dict:
  return {k: _deserializer.deserialize(v) for k, v in item.items()}


class FakeDynamo:
  """minimal DynamoDB stand-in faithful to the contract `Storage` depends on:
  paged `scan` / `query` with `ExclusiveStartKey` and a `LastEvaluatedKey` whose
  shape matches the real service (base PK for a scan, base PK + index PK/SK for a
  GSI query). Items are stored deserialized and (de)serialized at the boundary.
  """

  def __init__(self, items: list[dict]):
    self._items = list(items)

  def scan(self, **kwargs) -> dict:
    table_items = sorted(self._items, key=lambda it: it['trail_id'])
    return self._page(table_items, kwargs, key_attrs=['trail_id'])

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


def _trail(idx: int, *, bro: str, parent: str | None) -> dict:
  item = {
    'trail_id': f'trail-{idx:03d}',
    'bro': bro,
    'started_at': f'2026-06-07T00:00:{idx:02d}.000000Z',
  }
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


def _store() -> Storage:
  return Storage(
    dynamo=FakeDynamo(_TRAILS),
    s3=None,
    trails_table='trails',
    steps_table='trail_steps',
    bucket='bucket',
  )


async def _collect(
  store: Storage, *, bro=None, parent=None, limit: int
) -> tuple[list[dict], list[str]]:
  """paginate to exhaustion, returning the gathered trails and the per-page
  `next` cursors — mirrors `TrailsClient.iter_trails`.
  """
  trails: list[dict] = []
  cursors: list[str] = []
  cursor: str | None = None
  while True:
    page = await store.list_trails(
      bro=bro, parent=parent, since=None, until=None, cursor=cursor, limit=limit
    )
    trails.extend(page['trails'])
    cursor = page['next']
    if cursor is None:
      break
    cursors.append(cursor)
  return trails, cursors


def _ids(trails: list[dict]) -> list[str]:
  return [t['trail_id'] for t in trails]


@pytest.mark.asyncio
async def test_scan_pagination_round_trips():
  store = _store()
  single, _ = await _collect(store, limit=100)
  paged, _ = await _collect(store, limit=2)
  # pagination must neither drop, duplicate, nor reorder.
  assert _ids(paged) == _ids(single)
  assert _ids(single) == sorted(_ids(single))
  assert len(single) == len(_TRAILS)


@pytest.mark.asyncio
async def test_bro_pagination_round_trips():
  store = _store()
  single, _ = await _collect(store, bro='dev', limit=100)
  paged, _ = await _collect(store, bro='dev', limit=2)
  assert _ids(paged) == _ids(single)
  # newest first, only the 'dev' trails.
  assert _ids(single) == ['trail-004', 'trail-003', 'trail-002', 'trail-001', 'trail-000']


@pytest.mark.asyncio
async def test_parent_pagination_round_trips():
  store = _store()
  single, _ = await _collect(store, parent='P1', limit=100)
  paged, _ = await _collect(store, parent='P1', limit=2)
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
async def test_scan_cursor_is_a_json_object():
  store = _store()
  page = await store.list_trails(
    bro=None, parent=None, since=None, until=None, cursor=None, limit=2
  )
  assert json.loads(page['next']) == {'trail_id': 'trail-001'}
