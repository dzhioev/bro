"""Aggregate repair, verification, audits, and manifested relinking."""

import asyncio
import json
from collections.abc import Callable
from typing import Any, Optional

from bro.trails.server import storage_types
from bro.trails.server.folding import AggregateState

_ROW_STORAGE_FIELDS = frozenset(
  {
    'trail_id',
    'step_id',
    'ts',
    'kind',
    'body',
    'body_s3',
    'body_encoding',
    'usage',
    'payload_sha256',
  }
)
_ddb = storage_types.ddb
_ddb_item = storage_types.ddb_item
_from_ddb_item = storage_types.from_ddb_item


class Operations:
  def __init__(
    self,
    *,
    dynamo,
    s3,
    trails_table: str,
    steps_table: str,
    bucket: str,
    backend: Callable[[str], Any],
    required_header: Callable[[str], Any],
    materialize_row: Callable[..., Any],
  ):
    self._dynamo = dynamo
    self._s3 = s3
    self._trails_table = trails_table
    self._steps_table = steps_table
    self._bucket = bucket
    self._backend = backend
    self._required_header = required_header
    self._materialize_row = materialize_row

  async def recompute(self, trail_id: str) -> dict:
    header = await self._required_migrated_header(trail_id)
    rows = await self._all_universal_rows(trail_id)
    computed = await self._compute(header, rows)
    for row, expected in zip(rows, computed['rows'], strict=True):
      updated = {key: value for key, value in row.items() if key in _ROW_STORAGE_FIELDS}
      updated.pop('usage', None)
      updated['kind'] = expected['kind']
      updated.update(expected['attributes'])
      if expected['usage'] is not None:
        updated['usage'] = expected['usage']
      await asyncio.to_thread(
        self._dynamo.put_item,
        TableName=self._steps_table,
        Item=_ddb_item(updated),
      )
    await self._write_recomputed_header(header, computed)
    return {
      'trail_id': trail_id,
      'extent': computed['extent'],
      'turn_count': computed['turn_count'],
      'usage': computed['native']['usage'],
    }

  async def check(self, trail_id: Optional[str] = None) -> dict:
    if trail_id is not None:
      result = await self._check_trail(trail_id)
      return {'ok': result['ok'], 'trails': [result], 'cross_trail_duplicate_uuids': []}
    headers = await self._scan_items(self._trails_table)
    migrated = [
      header
      for header in headers
      if header.get('body_storage') == storage_types.UNIVERSAL_BODY_STORAGE
    ]
    results = [await self._check_trail(header['id']) for header in migrated]
    duplicates = await self._cross_trail_duplicate_uuids()
    return {
      'ok': all(result['ok'] for result in results) and len(duplicates) == 0,
      'trails': results,
      'cross_trail_duplicate_uuids': duplicates,
    }

  async def _check_trail(self, trail_id: str) -> dict:
    header = await self._required_migrated_header(trail_id)
    rows = await self._all_universal_rows(trail_id)
    computed = await self._compute(header, rows)
    differences: list[dict] = []
    for field in ('extent', 'turn_count', 'last_billed_message_id'):
      stored = header.get(field)
      expected = computed.get(field)
      if stored != expected:
        differences.append({'field': field, 'stored': stored, 'expected': expected})
    stored_native = header.get('native', {})
    for field, expected in computed['native'].items():
      stored = stored_native.get(field)
      if field in {'usage', 'step_counts_by_kind'}:
        stored = stored if stored is not None else {}
        expected = expected if expected is not None else {}
      if stored != expected:
        differences.append({'field': f'native.{field}', 'stored': stored, 'expected': expected})
    differences.extend(computed['row_differences'])
    differences.extend(computed['billing_differences'])
    return {'trail_id': trail_id, 'ok': len(differences) == 0, 'differences': differences}

  async def _compute(self, header: dict, rows: list[dict]) -> dict:
    native = {
      key: value
      for key, value in header.get('native', {}).items()
      if key not in {'usage', 'step_counts_by_kind'}
    }
    state = AggregateState({'native': native, 'turn_count': 0})
    adapter = self._backend(header['harness'])
    seen_billing_keys: set[str] = set()
    expected_rows: list[dict] = []
    row_differences: list[dict] = []
    billing_counts: dict[str, int] = {}
    for expected_step_id, row in enumerate(rows):
      if row.get('step_id') != expected_step_id:
        row_differences.append(
          {
            'step_id': row.get('step_id'),
            'field': 'step_id',
            'expected': expected_step_id,
          }
        )
      materialized = await self._materialize_row(header['harness'], row, resolve_large=True)
      parsed = adapter.parse(materialized)
      classification = adapter.classify(parsed)
      contribution = state.apply(parsed, classification, seen_billing_keys)
      expected_rows.append(
        {'kind': parsed.kind, 'attributes': parsed.attributes, 'usage': contribution}
      )
      if row.get('kind') != parsed.kind:
        row_differences.append(
          {'step_id': row.get('step_id'), 'field': 'kind', 'expected': parsed.kind}
        )
      stored_attributes = {
        key: value for key, value in row.items() if key not in _ROW_STORAGE_FIELDS
      }
      for key in sorted(set(stored_attributes) | set(parsed.attributes)):
        if stored_attributes.get(key) != parsed.attributes.get(key):
          row_differences.append(
            {
              'step_id': row.get('step_id'),
              'field': key,
              'stored': stored_attributes.get(key),
              'expected': parsed.attributes.get(key),
            }
          )
      stored_usage = row.get('usage')
      if stored_usage != contribution:
        row_differences.append(
          {
            'step_id': row.get('step_id'),
            'field': 'usage',
            'stored': stored_usage,
            'expected': contribution,
          }
        )
      if classification.usage is not None and classification.billing_key is not None:
        key = classification.billing_key
        billing_counts.setdefault(key, 0)
        if isinstance(stored_usage, dict):
          billing_counts[key] += 1
    billing_differences = [
      {'message_id': key, 'field': 'billing_contributions', 'stored': count, 'expected': 1}
      for key, count in billing_counts.items()
      if count != 1
    ]
    return {
      'extent': len(rows),
      'turn_count': state.turn_count,
      'last_billed_message_id': state.last_billed_message_id,
      'native': state.native,
      'subject': state.subject,
      'rows': expected_rows,
      'row_differences': row_differences,
      'billing_differences': billing_differences,
    }

  async def _write_recomputed_header(self, header: dict, computed: dict) -> None:
    names = {
      '#body_storage': 'body_storage',
      '#extent': 'extent',
      '#turn_count': 'turn_count',
      '#native': 'native',
      '#last_billed': 'last_billed_message_id',
    }
    values = {
      ':storage': _ddb(storage_types.UNIVERSAL_BODY_STORAGE),
      ':extent': _ddb(computed['extent']),
      ':turn_count': _ddb(computed['turn_count']),
      ':native': _ddb(computed['native']),
      ':last_billed': _ddb(computed['last_billed_message_id']),
    }
    assignments = [
      '#extent = :extent',
      '#turn_count = :turn_count',
      '#native = :native',
      '#last_billed = :last_billed',
    ]
    if computed['subject'] is not None:
      names['#subject'] = 'subject'
      values[':subject'] = _ddb(computed['subject'])
      assignments.append('#subject = if_not_exists(#subject, :subject)')
    await asyncio.to_thread(
      self._dynamo.update_item,
      TableName=self._trails_table,
      Key=_ddb_item({'id': header['id']}),
      ConditionExpression='#body_storage = :storage',
      UpdateExpression='SET ' + ', '.join(assignments),
      ExpressionAttributeNames=names,
      ExpressionAttributeValues=values,
    )

  async def relink(self, trail_id: str, forked_from: dict, delete_count: int) -> dict:
    if delete_count < 0:
      raise ValueError('delete_count must be non-negative')
    header = await self._required_migrated_header(trail_id)
    if header.get('forked_from') is not None:
      raise ValueError('trail already has forked_from')
    rows = await self._all_universal_rows(trail_id)
    if delete_count > len(rows):
      raise ValueError('delete_count exceeds the trail extent')
    timestamp = storage_types.now_iso()
    manifest_key = storage_types.relink_manifest_key(trail_id, timestamp)
    deleted = [
      await self._materialize_row(header['harness'], row, resolve_large=True)
      for row in rows[:delete_count]
    ]
    manifest = {
      'operation': 'relink',
      'at': timestamp,
      'trail_id': trail_id,
      'forked_from': forked_from,
      'delete_count': delete_count,
      'old_extent': len(rows),
      'new_extent': len(rows) - delete_count,
      'deleted_rows': deleted,
    }
    await asyncio.to_thread(
      self._s3.put_object,
      Bucket=self._bucket,
      Key=manifest_key,
      Body=json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
      ContentType='application/json',
    )

    remaining = rows[delete_count:]
    for step_id, row in enumerate(remaining):
      rewritten = {**row, 'step_id': step_id}
      await asyncio.to_thread(
        self._dynamo.put_item,
        TableName=self._steps_table,
        Item=_ddb_item(rewritten),
      )
    for step_id in range(len(remaining), len(rows)):
      await asyncio.to_thread(
        self._dynamo.delete_item,
        TableName=self._steps_table,
        Key=_ddb_item({'trail_id': trail_id, 'step_id': step_id}),
      )
    await self.recompute(trail_id)
    try:
      await asyncio.to_thread(
        self._dynamo.update_item,
        TableName=self._trails_table,
        Key=_ddb_item({'id': trail_id}),
        ConditionExpression='attribute_not_exists(#forked_from)',
        UpdateExpression='SET #forked_from = :forked_from, #forked_from_id = :forked_from_id',
        ExpressionAttributeNames={
          '#forked_from': 'forked_from',
          '#forked_from_id': 'forked_from_id',
        },
        ExpressionAttributeValues={
          ':forked_from': _ddb(forked_from),
          ':forked_from_id': _ddb(forked_from['trail_id']),
        },
      )
    except self._dynamo.exceptions.ConditionalCheckFailedException as exception:
      raise ValueError('trail already has forked_from') from exception
    return {
      'trail_id': trail_id,
      'forked_from': forked_from,
      'extent': len(remaining),
      'manifest_s3': manifest_key,
    }

  async def repair_llm_spec(self, trail_id: str, expected: Any, replacement: dict) -> dict:
    """replace a header's `native.llm` with `replacement`, but only while it still
    equals `expected` — the manifested repair for a launch recipe recorded under a
    vocabulary the current code no longer reads.

    `native` is otherwise immutable: what a writer recorded is what the trail says
    it ran. So this states the before-value it is replacing, writes it to S3 as a
    manifest, and applies the change under a condition on that same value — a
    second run, or a racing writer, leaves the header alone and says so.
    """
    header = await self._required_header(trail_id)
    native = header.get('native')
    if not isinstance(native, dict):
      raise ValueError('trail header has no native record')
    current = native.get('llm')
    if current != expected:
      raise ValueError(f'native.llm is not the expected value; found {json.dumps(current)}')
    timestamp = storage_types.now_iso()
    manifest_key = storage_types.llm_spec_manifest_key(trail_id, timestamp)
    manifest = {
      'operation': 'repair_llm_spec',
      'at': timestamp,
      'trail_id': trail_id,
      'harness': header.get('harness'),
      'previous': current,
      'replacement': replacement,
    }
    await asyncio.to_thread(
      self._s3.put_object,
      Bucket=self._bucket,
      Key=manifest_key,
      Body=json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
      ContentType='application/json',
    )
    try:
      await asyncio.to_thread(
        self._dynamo.update_item,
        TableName=self._trails_table,
        Key=_ddb_item({'id': trail_id}),
        ConditionExpression='#native.#llm = :expected',
        UpdateExpression='SET #native.#llm = :replacement',
        ExpressionAttributeNames={'#native': 'native', '#llm': 'llm'},
        ExpressionAttributeValues={
          ':expected': _ddb(expected),
          ':replacement': _ddb(replacement),
        },
      )
    except self._dynamo.exceptions.ConditionalCheckFailedException as exception:
      raise ValueError('native.llm changed before the repair applied') from exception
    return {
      'trail_id': trail_id,
      'previous': current,
      'llm': replacement,
      'manifest_s3': manifest_key,
    }

  async def _required_migrated_header(self, trail_id: str) -> dict:
    header = await self._required_header(trail_id)
    if header.get('body_storage') != storage_types.UNIVERSAL_BODY_STORAGE:
      raise ValueError('operation requires a migrated trail body')
    return header

  async def _all_universal_rows(self, trail_id: str) -> list[dict]:
    rows: list[dict] = []
    exclusive_start_key: Optional[dict] = None
    while True:
      kwargs: dict[str, Any] = {
        'TableName': self._steps_table,
        'KeyConditionExpression': 'trail_id = :trail_id',
        'ExpressionAttributeValues': {':trail_id': _ddb(trail_id)},
      }
      if exclusive_start_key is not None:
        kwargs['ExclusiveStartKey'] = exclusive_start_key
      response = await asyncio.to_thread(self._dynamo.query, **kwargs)
      rows.extend(
        row for item in response.get('Items', []) if (row := _from_ddb_item(item)) is not None
      )
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return rows

  async def _scan_items(self, table: str) -> list[dict]:
    items: list[dict] = []
    exclusive_start_key: Optional[dict] = None
    while True:
      kwargs: dict[str, Any] = {'TableName': table}
      if exclusive_start_key is not None:
        kwargs['ExclusiveStartKey'] = exclusive_start_key
      response = await asyncio.to_thread(self._dynamo.scan, **kwargs)
      items.extend(
        item for raw in response.get('Items', []) if (item := _from_ddb_item(raw)) is not None
      )
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return items

  async def _cross_trail_duplicate_uuids(self) -> list[dict]:
    trails_by_uuid: dict[str, set[str]] = {}
    exclusive_start_key: Optional[dict] = None
    while True:
      kwargs: dict[str, Any] = {
        'TableName': self._steps_table,
        'ProjectionExpression': 'trail_id, #uuid',
        'ExpressionAttributeNames': {'#uuid': 'uuid'},
      }
      if exclusive_start_key is not None:
        kwargs['ExclusiveStartKey'] = exclusive_start_key
      response = await asyncio.to_thread(self._dynamo.scan, **kwargs)
      for raw in response.get('Items', []):
        row = _from_ddb_item(raw)
        if row is None:
          continue
        uuid = row.get('uuid')
        trail_id = row.get('trail_id')
        if isinstance(uuid, str) and isinstance(trail_id, str):
          trails_by_uuid.setdefault(uuid, set()).add(trail_id)
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        break
    return [
      {'uuid': uuid, 'trail_ids': sorted(trail_ids)}
      for uuid, trail_ids in sorted(trails_by_uuid.items())
      if len(trail_ids) > 1
    ]
