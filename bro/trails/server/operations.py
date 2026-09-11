"""Aggregate repair, verification, audits, and the manifested destructive operations."""

import json
from collections.abc import Callable
from typing import Any, Optional

from bro.trails import formats, importing, rows
from bro.trails.server import dynamo_types
from bro.trails.store import delete_manifest

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
    'format',
  }
)
_ddb = dynamo_types.ddb
_ddb_item = dynamo_types.ddb_item
_from_ddb_item = dynamo_types.from_ddb_item


def _absent_billing_id(fields: dict) -> tuple[str, ...]:
  """The attribute to drop when the fold has no billing id: absent on every
  backend rather than null on this one."""
  return () if 'last_billed_message_id' in fields else ('last_billed_message_id',)


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
    resolve_row_body: Callable[..., Any],
  ):
    self._dynamo = dynamo
    self._s3 = s3
    self._trails_table = trails_table
    self._steps_table = steps_table
    self._bucket = bucket
    self._backend = backend
    self._required_header = required_header
    self._resolve_row_body = resolve_row_body

  def recompute(self, trail_id: str) -> dict:
    header = self._required_header(trail_id)
    stored = self._all_rows(trail_id)
    computed = self._compute(header, stored)
    for row, expected in zip(stored, computed['rows'], strict=True):
      updated = {key: value for key, value in row.items() if key in _ROW_STORAGE_FIELDS}
      updated.pop('usage', None)
      updated['kind'] = expected.record.kind
      updated.update(expected.record.attributes)
      if expected.usage is not None:
        updated['usage'] = expected.usage
      self._dynamo.put_item(
        TableName=self._steps_table,
        Item=_ddb_item(updated),
      )
    fields = computed['fields']
    self._write_fold(trail_id, fields, remove=_absent_billing_id(fields))
    return {
      'trail_id': trail_id,
      'extent': fields['extent'],
      'turn_count': fields['turn_count'],
      'usage': fields['native']['usage'],
    }

  def seal(self, header: dict, end: Optional[dict]) -> dict:
    """Refold the header from every stored row and record `end`."""
    trail_id = header['id']
    adapter = self._backend(header['harness'])
    resolved = [self._resolve_row_body(header['harness'], row) for row in self._all_rows(trail_id)]
    fields = importing.sealed_fields(header, resolved, adapter, end)
    try:
      self._write_fold(
        trail_id,
        fields,
        remove=('importing', *_absent_billing_id(fields)),
        expected_extent=len(resolved),
      )
    except self._dynamo.exceptions.ConditionalCheckFailedException as exception:
      raise ValueError(f'trail {trail_id} advanced while sealing') from exception
    return {'trail_id': trail_id, 'extent': len(resolved)}

  def check(self, trail_id: Optional[str] = None) -> dict:
    if trail_id is not None:
      result = self._check_trail(trail_id)
      return {'ok': result['ok'], 'trails': [result], 'cross_trail_duplicate_uuids': []}
    headers = self._scan_items(self._trails_table)
    results = [self._check_trail(header['id']) for header in headers]
    duplicates = self._cross_trail_duplicate_uuids()
    return {
      'ok': all(result['ok'] for result in results) and len(duplicates) == 0,
      'trails': results,
      'cross_trail_duplicate_uuids': duplicates,
    }

  def _check_trail(self, trail_id: str) -> dict:
    header = self._required_header(trail_id)
    served_header = formats.upgrade_header(header)
    stored = self._all_rows(trail_id)
    computed = self._compute(served_header, stored)
    fields = computed['fields']
    differences: list[dict] = []
    for field in ('extent', 'turn_count', 'last_billed_message_id'):
      stored_value = served_header.get(field)
      expected = fields.get(field)
      if stored_value != expected:
        differences.append({'field': field, 'stored': stored_value, 'expected': expected})
    stored_native = served_header.get('native', {})
    for field, expected in fields['native'].items():
      stored_value = stored_native.get(field)
      if field in {'usage', 'step_counts_by_kind'}:
        stored_value = stored_value if stored_value is not None else {}
        expected = expected if expected is not None else {}
      if stored_value != expected:
        differences.append(
          {'field': f'native.{field}', 'stored': stored_value, 'expected': expected}
        )
    differences.extend(computed['row_differences'])
    differences.extend(computed['billing_differences'])
    return {'trail_id': trail_id, 'ok': len(differences) == 0, 'differences': differences}

  def _compute(self, header: dict, stored: list[dict]) -> dict:
    header = formats.upgrade_header(header)
    adapter = self._backend(header['harness'])
    resolved = [self._resolve_row_body(header['harness'], row) for row in stored]
    state, replayed = rows.replay(header, resolved, adapter)
    row_differences: list[dict] = []
    billing_counts: dict[str, int] = {}
    for expected_step_id, (row, resolved_row, expected) in enumerate(
      zip(stored, resolved, replayed, strict=True)
    ):
      if row.get('step_id') != expected_step_id:
        row_differences.append(
          {
            'step_id': row.get('step_id'),
            'field': 'step_id',
            'expected': expected_step_id,
          }
        )
      parsed = expected.record
      if resolved_row.get('kind') != parsed.kind:
        row_differences.append(
          {'step_id': row.get('step_id'), 'field': 'kind', 'expected': parsed.kind}
        )
      stored_attributes = {
        key: value for key, value in resolved_row.items() if key not in _ROW_STORAGE_FIELDS
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
      stored_usage = resolved_row.get('usage')
      if stored_usage != expected.usage:
        row_differences.append(
          {
            'step_id': row.get('step_id'),
            'field': 'usage',
            'stored': stored_usage,
            'expected': expected.usage,
          }
        )
      classification = expected.classification
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
    fields = rows.state_fields(state, len(stored))
    return {
      'fields': fields,
      'rows': replayed,
      'row_differences': row_differences,
      'billing_differences': billing_differences,
    }

  def _write_fold(
    self,
    trail_id: str,
    fields: dict,
    *,
    remove: tuple[str, ...] = (),
    expected_extent: Optional[int] = None,
  ) -> None:
    """Write the fold-owned header fields and drop the attributes in `remove`;
    a subject only where none is stored."""
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    assignments: list[str] = []
    for index, (key, value) in enumerate(fields.items()):
      name = f'#field{index}'
      placeholder = f':field{index}'
      names[name] = key
      values[placeholder] = _ddb(value)
      if key == 'subject':
        assignments.append(f'{name} = if_not_exists({name}, {placeholder})')
      else:
        assignments.append(f'{name} = {placeholder}')
    removals: list[str] = []
    for index, key in enumerate(remove):
      name = f'#remove{index}'
      names[name] = key
      removals.append(name)
    update = 'SET ' + ', '.join(assignments)
    if len(removals) > 0:
      update += ' REMOVE ' + ', '.join(removals)
    condition = 'attribute_exists(id)'
    if expected_extent is not None:
      names['#extent'] = 'extent'
      values[':expected_extent'] = _ddb(expected_extent)
      condition += ' AND #extent = :expected_extent'
    self._dynamo.update_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConditionExpression=condition,
      UpdateExpression=update,
      ExpressionAttributeNames=names,
      ExpressionAttributeValues=values,
    )

  def relink(self, trail_id: str, forked_from: dict, delete_count: int) -> dict:
    if delete_count < 0:
      raise ValueError('delete_count must be non-negative')
    header = self._required_header(trail_id)
    if header.get('forked_from') is not None:
      raise ValueError('trail already has forked_from')
    stored = self._all_rows(trail_id)
    if delete_count > len(stored):
      raise ValueError('delete_count exceeds the trail extent')
    timestamp = dynamo_types.now_iso()
    manifest_key = dynamo_types.relink_manifest_key(trail_id, timestamp)
    deleted = [self._resolve_row_body(header['harness'], row) for row in stored[:delete_count]]
    manifest = {
      'operation': 'relink',
      'at': timestamp,
      'trail_id': trail_id,
      'forked_from': forked_from,
      'delete_count': delete_count,
      'old_extent': len(stored),
      'new_extent': len(stored) - delete_count,
      'deleted_rows': deleted,
    }
    self._s3.put_object(
      Bucket=self._bucket,
      Key=manifest_key,
      Body=json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
      ContentType='application/json',
    )

    remaining = stored[delete_count:]
    for step_id, row in enumerate(remaining):
      rewritten = {**row, 'step_id': step_id}
      self._dynamo.put_item(
        TableName=self._steps_table,
        Item=_ddb_item(rewritten),
      )
    for step_id in range(len(remaining), len(stored)):
      self._dynamo.delete_item(
        TableName=self._steps_table,
        Key=_ddb_item({'trail_id': trail_id, 'step_id': step_id}),
      )
    self.recompute(trail_id)
    try:
      self._dynamo.update_item(
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

  def delete_trail(self, header: dict) -> dict:
    """Remove a trail's rows, spilled bodies, launch context and header, in that
    order — the header goes last so an interrupted delete leaves a trail the same
    call finishes off rather than rows nothing points at."""
    trail_id = header['id']
    stored = self._all_rows(trail_id)
    timestamp = dynamo_types.now_iso()
    manifest_key = dynamo_types.delete_manifest_key(trail_id, timestamp)
    self._s3.put_object(
      Bucket=self._bucket,
      Key=manifest_key,
      Body=json.dumps(
        delete_manifest(
          trail_id=trail_id,
          at=timestamp,
          header=header,
          steps=[self._resolve_row_body(header['harness'], row) for row in stored],
        ),
        ensure_ascii=False,
      ).encode('utf-8'),
      ContentType='application/json',
    )
    for row in stored:
      self._dynamo.delete_item(
        TableName=self._steps_table,
        Key=_ddb_item({'trail_id': trail_id, 'step_id': row['step_id']}),
      )
    # tool blobs are content-addressed and shared across trails, so they are not
    # this trail's to remove
    objects = [row['body_s3'] for row in stored if row.get('body_s3') is not None]
    context = header.get('context_s3') or header.get('native', {}).get('context_s3')
    if context is not None:
      objects.append(context)
    for key in objects:
      self._s3.delete_object(Bucket=self._bucket, Key=key)
    self._dynamo.delete_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
    )
    return {'trail_id': trail_id, 'extent': len(stored), 'manifest': manifest_key}

  def _all_rows(self, trail_id: str) -> list[dict]:
    stored: list[dict] = []
    exclusive_start_key: Optional[dict] = None
    while True:
      kwargs: dict[str, Any] = {
        'TableName': self._steps_table,
        'KeyConditionExpression': 'trail_id = :trail_id',
        'ExpressionAttributeValues': {':trail_id': _ddb(trail_id)},
      }
      if exclusive_start_key is not None:
        kwargs['ExclusiveStartKey'] = exclusive_start_key
      response = self._dynamo.query(**kwargs)
      stored.extend(
        row for item in response.get('Items', []) if (row := _from_ddb_item(item)) is not None
      )
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return stored

  def _scan_items(self, table: str) -> list[dict]:
    items: list[dict] = []
    exclusive_start_key: Optional[dict] = None
    while True:
      kwargs: dict[str, Any] = {'TableName': table}
      if exclusive_start_key is not None:
        kwargs['ExclusiveStartKey'] = exclusive_start_key
      response = self._dynamo.scan(**kwargs)
      items.extend(
        item for raw in response.get('Items', []) if (item := _from_ddb_item(raw)) is not None
      )
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return items

  def _cross_trail_duplicate_uuids(self) -> list[dict]:
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
      response = self._dynamo.scan(**kwargs)
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
