"""Aggregate repair, verification, audits, and the manifested destructive operations."""

import json
from collections.abc import Callable
from typing import Any, Optional

from bro.trails.lineage import LineageHead, walk_header_chain
from bro.trails.rows import AggregateState
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
  }
)
_ddb = dynamo_types.ddb
_ddb_item = dynamo_types.ddb_item
_from_ddb_item = dynamo_types.from_ddb_item


def _row_digest(row: dict) -> str:
  digest = row.get('payload_sha256')
  if not isinstance(digest, str):
    raise ValueError(f'step {row.get("trail_id")}/{row.get("step_id")} carries no payload digest')
  return digest


def _migrated(header: dict) -> bool:
  return header.get('body_storage') == dynamo_types.UNIVERSAL_BODY_STORAGE


def _folded_head(rows: list[dict]) -> LineageHead:
  """The head a trail's rows fold to, from their identities alone."""
  head = LineageHead()
  for step_id, row in enumerate(rows):
    uuid = row.get('uuid')
    head.fold(
      step_id=step_id,
      uuid=uuid if isinstance(uuid, str) else None,
      payload_sha256=_row_digest(row),
    )
  return head


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
    header = self._required_migrated_header(trail_id)
    rows = self._all_universal_rows(trail_id)
    computed = self._compute(header, rows)
    for row, expected in zip(rows, computed['rows'], strict=True):
      updated = {key: value for key, value in row.items() if key in _ROW_STORAGE_FIELDS}
      updated.pop('usage', None)
      updated['kind'] = expected['kind']
      updated.update(expected['attributes'])
      if expected['usage'] is not None:
        updated['usage'] = expected['usage']
      self._dynamo.put_item(
        TableName=self._steps_table,
        Item=_ddb_item(updated),
      )
    self._write_recomputed_header(header, computed)
    return {
      'trail_id': trail_id,
      'extent': computed['extent'],
      'turn_count': computed['turn_count'],
      'usage': computed['native']['usage'],
    }

  def check(self, trail_id: Optional[str] = None) -> dict:
    if trail_id is not None:
      result = self._check_trail(trail_id)
      return {'ok': result['ok'], 'trails': [result], 'cross_trail_duplicate_uuids': []}
    headers = self._scan_items(self._trails_table)
    migrated = [header for header in headers if _migrated(header)]
    results = [self._check_trail(header['id']) for header in migrated]
    duplicates = self._cross_trail_duplicate_uuids()
    return {
      'ok': all(result['ok'] for result in results) and len(duplicates) == 0,
      'trails': results,
      'cross_trail_duplicate_uuids': duplicates,
    }

  def _check_trail(self, trail_id: str) -> dict:
    header = self._required_migrated_header(trail_id)
    rows = self._all_universal_rows(trail_id)
    computed = self._compute(header, rows)
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

  def _compute(self, header: dict, rows: list[dict]) -> dict:
    adapter = self._backend(header['harness'])
    state = AggregateState.replaying(header, adapter)
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
      resolved = self._resolve_row_body(header['harness'], row)
      parsed = adapter.parse(resolved)
      classification = adapter.classify(parsed)
      contribution = state.apply(
        parsed,
        classification,
        seen_billing_keys,
        step_id=expected_step_id,
        digest=_row_digest(row),
      )
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

  def backfill_lineage_heads(self) -> dict:
    """Fold `native.lineage_head` and stamp the segment index key onto the claude
    trails recorded before an append transaction carried them."""
    headers = {header['id']: header for header in self._scan_items(self._trails_table)}
    identities: dict[str, list[dict]] = {}

    def rows_of(trail_id: str) -> list[dict]:
      if trail_id not in identities:
        identities[trail_id] = self._row_identities(trail_id)
      return identities[trail_id]

    def parent_header(trail_id: str) -> dict:
      header = headers.get(trail_id)
      if header is None:
        raise ValueError(f'trail {trail_id} is forked from a trail the registry has lost')
      return header

    stamped: list[str] = []
    contended: list[str] = []
    for trail_id, header in sorted(headers.items()):
      if header.get('harness') != 'claude' or not _migrated(header):
        continue
      rows = rows_of(trail_id)
      head = _folded_head(rows)
      root, _ = walk_header_chain(header, parent_header)[0]
      head.chain_first_uuid = _folded_head(rows_of(root['id'])).chain_first_uuid
      segment = header.get('native', {}).get('segment')
      written = self._write_lineage_head(trail_id, segment, head, len(rows))
      (stamped if written else contended).append(trail_id)
    return {'ok': len(contended) == 0, 'stamped': stamped, 'contended': contended}

  def _write_lineage_head(
    self, trail_id: str, segment: Optional[str], head: LineageHead, extent: int
  ) -> bool:
    """Write the head alone, conditional on the extent its rows were read at, so
    an append landing meanwhile keeps the aggregate it folded."""
    names = {'#native': 'native', '#lineage_head': 'lineage_head', '#extent': 'extent'}
    values = {':head': _ddb(head.fields()), ':extent': _ddb(extent)}
    assignments = ['#native.#lineage_head = :head']
    if segment is not None:
      names['#segment'] = 'segment'
      values[':segment'] = _ddb(segment)
      assignments.append('#segment = :segment')
    try:
      self._dynamo.update_item(
        TableName=self._trails_table,
        Key=_ddb_item({'id': trail_id}),
        ConditionExpression='#extent = :extent',
        UpdateExpression='SET ' + ', '.join(assignments),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
      )
    except self._dynamo.exceptions.ConditionalCheckFailedException:
      return False
    return True

  def _row_identities(self, trail_id: str) -> list[dict]:
    """Each row's step id, record uuid, and payload digest in step order — what
    the head folds, without reading a body."""
    rows: list[dict] = []
    exclusive_start_key: Optional[dict] = None
    while True:
      kwargs: dict[str, Any] = {
        'TableName': self._steps_table,
        'KeyConditionExpression': 'trail_id = :trail_id',
        'ProjectionExpression': 'trail_id, step_id, #uuid, payload_sha256',
        'ExpressionAttributeNames': {'#uuid': 'uuid'},
        'ExpressionAttributeValues': {':trail_id': _ddb(trail_id)},
        'ConsistentRead': True,
      }
      if exclusive_start_key is not None:
        kwargs['ExclusiveStartKey'] = exclusive_start_key
      response = self._dynamo.query(**kwargs)
      rows.extend(
        row for item in response.get('Items', []) if (row := _from_ddb_item(item)) is not None
      )
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return rows

  def _write_recomputed_header(self, header: dict, computed: dict) -> None:
    names = {
      '#body_storage': 'body_storage',
      '#extent': 'extent',
      '#turn_count': 'turn_count',
      '#native': 'native',
      '#last_billed': 'last_billed_message_id',
    }
    values = {
      ':storage': _ddb(dynamo_types.UNIVERSAL_BODY_STORAGE),
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
    self._dynamo.update_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': header['id']}),
      ConditionExpression='#body_storage = :storage',
      UpdateExpression='SET ' + ', '.join(assignments),
      ExpressionAttributeNames=names,
      ExpressionAttributeValues=values,
    )

  def relink(self, trail_id: str, forked_from: dict, delete_count: int) -> dict:
    if delete_count < 0:
      raise ValueError('delete_count must be non-negative')
    header = self._required_migrated_header(trail_id)
    if header.get('forked_from') is not None:
      raise ValueError('trail already has forked_from')
    rows = self._all_universal_rows(trail_id)
    if delete_count > len(rows):
      raise ValueError('delete_count exceeds the trail extent')
    timestamp = dynamo_types.now_iso()
    manifest_key = dynamo_types.relink_manifest_key(trail_id, timestamp)
    deleted = [self._resolve_row_body(header['harness'], row) for row in rows[:delete_count]]
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
    self._s3.put_object(
      Bucket=self._bucket,
      Key=manifest_key,
      Body=json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
      ContentType='application/json',
    )

    remaining = rows[delete_count:]
    for step_id, row in enumerate(remaining):
      rewritten = {**row, 'step_id': step_id}
      self._dynamo.put_item(
        TableName=self._steps_table,
        Item=_ddb_item(rewritten),
      )
    for step_id in range(len(remaining), len(rows)):
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
    rows = self._all_universal_rows(trail_id)
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
          steps=[self._resolve_row_body(header['harness'], row) for row in rows],
        ),
        ensure_ascii=False,
      ).encode('utf-8'),
      ContentType='application/json',
    )
    for row in rows:
      self._dynamo.delete_item(
        TableName=self._steps_table,
        Key=_ddb_item({'trail_id': trail_id, 'step_id': row['step_id']}),
      )
    # tool blobs are content-addressed and shared across trails, so they are not
    # this trail's to remove
    objects = [row['body_s3'] for row in rows if row.get('body_s3') is not None]
    context = header.get('context_s3') or header.get('native', {}).get('context_s3')
    if context is not None:
      objects.append(context)
    for key in objects:
      self._s3.delete_object(Bucket=self._bucket, Key=key)
    self._dynamo.delete_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
    )
    return {'trail_id': trail_id, 'extent': len(rows), 'manifest': manifest_key}

  def _required_migrated_header(self, trail_id: str) -> dict:
    header = self._required_header(trail_id)
    if not _migrated(header):
      raise ValueError('operation requires a migrated trail body')
    return header

  def _all_universal_rows(self, trail_id: str) -> list[dict]:
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
      response = self._dynamo.query(**kwargs)
      rows.extend(
        row for item in response.get('Items', []) if (row := _from_ddb_item(item)) is not None
      )
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return rows

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
