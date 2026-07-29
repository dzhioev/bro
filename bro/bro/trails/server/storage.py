"""Universal trail headers, append storage, and reads."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from trails.model import UNREPORTED_END_INFERENCE, canonical_json_bytes
from trails.server import backends, row_storage, storage_types
from trails.server.folding import AggregateState
from trails.server.operations import Operations

SPILLOVER_THRESHOLD_BYTES = storage_types.SPILLOVER_THRESHOLD_BYTES
MAX_BODY_BYTES = storage_types.MAX_BODY_BYTES
INLINE_RESPONSE_THRESHOLD_BYTES = storage_types.INLINE_RESPONSE_THRESHOLD_BYTES
PRESIGNED_URL_TTL_SECONDS = storage_types.PRESIGNED_URL_TTL_SECONDS
TrailNotFound = storage_types.TrailNotFound
BodyTooLarge = storage_types.BodyTooLarge
AppendConflict = storage_types.AppendConflict

GSI_PK_ATTRIBUTE = 'gsi_pk'
GSI_PK_VALUE = 'trail'
UNREPORTED_AFTER_SECONDS = 3600
SWEEP_WINDOW_DAYS = 30
_ddb = storage_types.ddb
_ddb_item = storage_types.ddb_item
_from_ddb = storage_types.from_ddb
_from_ddb_item = storage_types.from_ddb_item


class Storage:
  def __init__(
    self,
    *,
    dynamo,
    s3,
    trails_table: str,
    steps_table: str,
    bucket: str,
    uuid_index: Optional[str] = None,
  ):
    self._dynamo = dynamo
    self._s3 = s3
    self._trails_table = trails_table
    self._steps_table = steps_table
    self._bucket = bucket
    self._uuid_index = uuid_index
    self._backends = dict(backends.BACKENDS)
    self._stored_tool_hashes: set[str] = set()
    self._operations = Operations(
      dynamo=dynamo,
      s3=s3,
      trails_table=trails_table,
      steps_table=self._steps_table,
      bucket=bucket,
      backend=self._backend,
      required_header=self._required_header,
      materialize_row=self._materialize_row,
    )

  def _backend(self, harness: str) -> backends.Adapter:
    try:
      return self._backends[harness]
    except KeyError as exception:
      raise ValueError(f'unsupported harness: {harness}') from exception

  def validate_create(self, harness: str, native: dict) -> None:
    if not isinstance(native, dict):
      raise ValueError('native must be an object')
    self._backend(harness).validate_create(native)

  async def create_trail(
    self,
    *,
    harness: str,
    version: str,
    interactive: bool,
    surface: str,
    body: dict,
    bro: Optional[str] = None,
    hold: Optional[str] = None,
    forked_from: Optional[dict] = None,
    summoned_by: Optional[dict] = None,
    subject: Optional[str] = None,
    location: Optional[dict] = None,
    native: Optional[dict] = None,
    trail_id: Optional[str] = None,
  ) -> dict:
    native = native if native is not None else {}
    self.validate_create(harness, native)
    if harness == 'bro' and bro is None:
      raise ValueError('bro is required for the bro harness')
    trail_id = trail_id if trail_id is not None else storage_types.new_id()
    started_at = _now_iso()
    adapter = self._backend(harness)
    launch_context = body.get('launch_context')
    opened = adapter.open(body)
    if len(opened.records) > storage_types.MAX_TRANSACTION_RECORDS:
      raise ValueError(
        f'a trail may open with at most {storage_types.MAX_TRANSACTION_RECORDS} records'
      )
    if launch_context is not None:
      await self._store_context(trail_id, launch_context)

    item: dict[str, Any] = {
      'id': trail_id,
      'harness': harness,
      'version': version,
      'started_at': started_at,
      'end': None,
      'last_alive_at': started_at,
      'interactive': interactive,
      'surface': surface,
      'turn_count': 0,
      'native': dict(native),
      GSI_PK_ATTRIBUTE: GSI_PK_VALUE,
    }
    optional = {
      'bro': bro,
      'hold': hold,
      'forked_from': forked_from,
      'summoned_by': summoned_by,
      'subject': subject,
      'location': location,
      'context_s3': storage_types.context_key(trail_id) if launch_context is not None else None,
    }
    item.update({key: value for key, value in optional.items() if value is not None})
    if forked_from is not None:
      item['forked_from_id'] = forked_from['trail_id']

    item['body_storage'] = storage_types.UNIVERSAL_BODY_STORAGE
    item['extent'] = 0
    state = AggregateState(item)
    seen_billing_keys: set[str] = set()
    rows = await row_storage.prepare_rows(
      s3=self._s3,
      bucket=self._bucket,
      trail_id=trail_id,
      offset=0,
      payloads=opened.records,
      adapter=adapter,
      default_timestamp=started_at,
      state=state,
      seen_billing_keys=seen_billing_keys,
    )
    item.update(self._state_fields(state, len(rows)))
    await asyncio.to_thread(
      self._dynamo.transact_write_items,
      TransactItems=[
        {
          'Put': {
            'TableName': self._trails_table,
            'Item': _ddb_item(item),
            'ConditionExpression': 'attribute_not_exists(id)',
          }
        },
        *[
          {
            'Put': {
              'TableName': self._steps_table,
              'Item': _ddb_item(row),
              'ConditionExpression': 'attribute_not_exists(trail_id)',
            }
          }
          for row in rows
        ],
      ],
    )
    return {'id': trail_id, 'started_at': started_at}

  async def _store_context(self, trail_id: str, context: Any) -> None:
    await asyncio.to_thread(
      self._s3.put_object,
      Bucket=self._bucket,
      Key=storage_types.context_key(trail_id),
      Body=json.dumps(context, ensure_ascii=False).encode('utf-8'),
      ContentType='application/json',
    )

  async def append_records(
    self,
    trail_id: str,
    *,
    offset: int,
    records: list[Any],
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    if offset < 0:
      raise ValueError('offset must be non-negative')
    header = await self._required_universal_header(trail_id)
    actual = self._header_extent(header)
    expected_end = offset + len(records)
    if actual != offset:
      if actual == expected_end and await self._batch_matches(trail_id, offset, records):
        return {'extent': actual, 'appended': 0, 'duplicate': True}
      raise AppendConflict(offset, actual)
    await self._store_tools(tools if tools is not None else {})
    if len(records) == 0:
      return {'extent': actual, 'appended': 0}

    adapter = self._backend(header['harness'])
    state = AggregateState(header)
    seen_billing_keys: set[str] = set()
    committed = 0
    while committed < len(records):
      chunk = records[committed : committed + storage_types.MAX_TRANSACTION_RECORDS]
      chunk_offset = offset + committed
      rows = await row_storage.prepare_rows(
        s3=self._s3,
        bucket=self._bucket,
        trail_id=trail_id,
        offset=chunk_offset,
        payloads=chunk,
        adapter=adapter,
        default_timestamp=_now_iso(),
        state=state,
        seen_billing_keys=seen_billing_keys,
      )
      new_extent = chunk_offset + len(rows)
      update = self._append_header_update(
        trail_id,
        expected_extent=chunk_offset,
        state=state,
        new_extent=new_extent,
      )
      transaction_items = [
        {
          'Put': {
            'TableName': self._steps_table,
            'Item': _ddb_item(row),
            'ConditionExpression': 'attribute_not_exists(trail_id)',
          }
        }
        for row in rows
      ]
      transaction_items.append({'Update': update})
      try:
        await asyncio.to_thread(
          self._dynamo.transact_write_items,
          TransactItems=transaction_items,
        )
      except self._dynamo.exceptions.TransactionCanceledException as exception:
        refreshed = await self._required_header(trail_id)
        refreshed_extent = self._header_extent(refreshed)
        if (
          committed == 0
          and refreshed_extent == expected_end
          and await self._batch_matches(trail_id, offset, records)
        ):
          return {'extent': refreshed_extent, 'appended': 0, 'duplicate': True}
        if refreshed_extent != chunk_offset:
          raise AppendConflict(chunk_offset, refreshed_extent) from exception
        raise RuntimeError(
          f'ordinal {chunk_offset} is already occupied at the trail extent'
        ) from exception
      committed += len(rows)
    return {'extent': offset + committed, 'appended': committed}

  async def _batch_matches(self, trail_id: str, offset: int, records: list[Any]) -> bool:
    response = await asyncio.to_thread(
      self._dynamo.query,
      TableName=self._steps_table,
      KeyConditionExpression='trail_id = :trail_id AND step_id BETWEEN :start AND :end',
      ExpressionAttributeValues={
        ':trail_id': _ddb(trail_id),
        ':start': _ddb(offset),
        ':end': _ddb(offset + len(records) - 1),
      },
      ConsistentRead=True,
    )
    rows = [row for item in response.get('Items', []) if (row := _from_ddb_item(item)) is not None]
    expected = [storage_types.sha256_hex(canonical_json_bytes(record)) for record in records]
    return len(rows) == len(records) and all(
      row.get('payload_sha256') == sha256 for row, sha256 in zip(rows, expected, strict=True)
    )

  async def _store_tools(self, tools: dict[str, Any]) -> None:
    if not isinstance(tools, dict):
      raise ValueError('tools must be an object keyed by sha256')
    for sha256, body in tools.items():
      if not isinstance(sha256, str) or len(sha256) != 64:
        raise ValueError('tool blob keys must be sha256 hex strings')
      payload = canonical_json_bytes(body)
      if storage_types.sha256_hex(payload) != sha256:
        raise ValueError(f'tool blob hash mismatch: {sha256}')
      if sha256 in self._stored_tool_hashes:
        continue
      await asyncio.to_thread(
        self._s3.put_object,
        Bucket=self._bucket,
        Key=storage_types.tool_blob_key(sha256),
        Body=payload,
        ContentType='application/json',
      )
      self._stored_tool_hashes.add(sha256)

  @staticmethod
  def _state_fields(state: AggregateState, extent: int) -> dict:
    fields: dict[str, Any] = {
      'extent': extent,
      'turn_count': state.turn_count,
      'native': state.native,
    }
    if state.last_billed_message_id is not None:
      fields['last_billed_message_id'] = state.last_billed_message_id
    if state.subject is not None:
      fields['subject'] = state.subject
    return fields

  def _append_header_update(
    self,
    trail_id: str,
    *,
    expected_extent: int,
    state: AggregateState,
    new_extent: int,
  ) -> dict:
    names = {
      '#body_storage': 'body_storage',
      '#extent': 'extent',
      '#last_alive_at': 'last_alive_at',
      '#turn_count': 'turn_count',
      '#native': 'native',
      '#last_billed': 'last_billed_message_id',
    }
    values = {
      ':storage': _ddb(storage_types.UNIVERSAL_BODY_STORAGE),
      ':expected_extent': _ddb(expected_extent),
      ':extent': _ddb(new_extent),
      ':alive': _ddb(_now_iso()),
      ':turn_count': _ddb(state.turn_count),
      ':native': _ddb(state.native),
      ':last_billed': _ddb(state.last_billed_message_id),
    }
    assignments = [
      '#extent = :extent',
      '#last_alive_at = :alive',
      '#turn_count = :turn_count',
      '#native = :native',
      '#last_billed = :last_billed',
    ]
    if state.subject is not None:
      names['#subject'] = 'subject'
      values[':subject'] = _ddb(state.subject)
      assignments.append('#subject = if_not_exists(#subject, :subject)')
    return {
      'TableName': self._trails_table,
      'Key': _ddb_item({'id': trail_id}),
      'ConditionExpression': '#body_storage = :storage AND #extent = :expected_extent',
      'UpdateExpression': 'SET ' + ', '.join(assignments),
      'ExpressionAttributeNames': names,
      'ExpressionAttributeValues': values,
    }

  @staticmethod
  def _header_extent(header: dict) -> int:
    extent = header.get('extent')
    if not isinstance(extent, int) or isinstance(extent, bool) or extent < 0:
      raise ValueError('migrated trail header has an invalid extent')
    return extent

  async def update_header(self, trail_id: str, changes: dict) -> dict:
    header = await self._required_universal_header(trail_id)
    unknown = set(changes) - {'subject', 'last_alive_at'}
    if len(unknown) > 0:
      raise ValueError(f'immutable or unknown header fields: {sorted(unknown)}')
    names: dict[str, str] = {}
    values: dict[str, dict] = {}
    assignments: list[str] = []
    for index, (key, value) in enumerate(changes.items()):
      names[f'#field{index}'] = key
      values[f':value{index}'] = _ddb(value)
      assignments.append(f'#field{index} = :value{index}')
    if len(assignments) == 0:
      return self._project_header(header)
    await asyncio.to_thread(
      self._dynamo.update_item,
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConditionExpression='attribute_exists(id)',
      UpdateExpression='SET ' + ', '.join(assignments),
      ExpressionAttributeNames=names,
      ExpressionAttributeValues=values,
    )
    return self._project_header(await self._required_header(trail_id))

  async def end_trail(
    self,
    *,
    trail_id: str,
    reason: str,
    detail: Optional[str],
  ) -> dict:
    await self._required_header(trail_id)
    timestamp = _now_iso()
    end = {'at': timestamp, 'reason': reason}
    if detail is not None:
      end['detail'] = detail
    await asyncio.to_thread(
      self._dynamo.update_item,
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConditionExpression='attribute_exists(id)',
      UpdateExpression='SET #end = :end, last_alive_at = :timestamp',
      ExpressionAttributeNames={'#end': 'end'},
      ExpressionAttributeValues={':end': _ddb(end), ':timestamp': _ddb(timestamp)},
    )
    return {'ended_at': timestamp}

  async def keepalive(self, trail_id: str) -> dict:
    timestamp = _now_iso()
    try:
      await asyncio.to_thread(
        self._dynamo.update_item,
        TableName=self._trails_table,
        Key=_ddb_item({'id': trail_id}),
        ConditionExpression='attribute_exists(id)',
        UpdateExpression='SET last_alive_at = :timestamp',
        ExpressionAttributeValues={':timestamp': _ddb(timestamp)},
      )
    except self._dynamo.exceptions.ConditionalCheckFailedException as exception:
      raise TrailNotFound(trail_id) from exception
    return {'last_alive_at': timestamp}

  async def sweep_unreported(self) -> list[str]:
    now = datetime.now(UTC)
    cutoff = _format_iso(now - timedelta(seconds=UNREPORTED_AFTER_SECONDS))
    since = _format_iso(now - timedelta(days=SWEEP_WINDOW_DAYS))
    swept: list[str] = []
    cursor: Optional[str] = None
    while True:
      page = await self.list_trails(
        harness=None,
        bro=None,
        forked_from=None,
        since=since,
        until=None,
        cursor=cursor,
        limit=100,
        project=False,
      )
      for item in page['trails']:
        if item.get('end') is not None or item['last_alive_at'] >= cutoff:
          continue
        if await self._stamp_unreported(item['id'], item['last_alive_at']):
          swept.append(item['id'])
      cursor = page['next']
      if cursor is None:
        return swept

  async def _stamp_unreported(self, trail_id: str, ended_at: str) -> bool:
    try:
      await asyncio.to_thread(
        self._dynamo.update_item,
        TableName=self._trails_table,
        Key=_ddb_item({'id': trail_id}),
        ConditionExpression='attribute_type(#end, :null_type)',
        UpdateExpression='SET #end = :end',
        ExpressionAttributeNames={'#end': 'end'},
        ExpressionAttributeValues={
          ':end': _ddb({'at': ended_at, 'inference': UNREPORTED_END_INFERENCE}),
          ':null_type': _ddb('NULL'),
        },
      )
    except self._dynamo.exceptions.ConditionalCheckFailedException:
      return False
    return True

  async def get_trail(self, trail_id: str) -> Optional[dict]:
    response = await asyncio.to_thread(
      self._dynamo.get_item,
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
    )
    item = _from_ddb_item(response.get('Item'))
    return self._project_header(item) if item is not None else None

  async def _required_header(self, trail_id: str) -> dict:
    response = await asyncio.to_thread(
      self._dynamo.get_item,
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConsistentRead=True,
    )
    item = _from_ddb_item(response.get('Item'))
    if item is None:
      raise TrailNotFound(trail_id)
    return item

  async def _required_universal_header(self, trail_id: str) -> dict:
    header = await self._required_header(trail_id)
    if header.get('body_storage') != storage_types.UNIVERSAL_BODY_STORAGE:
      raise ValueError(f'trail {trail_id} has no body in {storage_types.UNIVERSAL_BODY_STORAGE}')
    return header

  def _project_header(self, item: dict) -> dict:
    raw_usage = item.get('native', {}).get('usage', {})
    if not isinstance(raw_usage, dict):
      raise ValueError('native.usage must be an object')
    return {**item, 'usage': raw_usage, 'models': sorted(raw_usage)}

  async def get_launch_context(self, trail_id: str) -> Optional[Any]:
    header = await self._required_header(trail_id)
    key = header.get('context_s3')
    if key is None:
      key = header.get('native', {}).get('context_s3')
    if key is None:
      return None
    response = await asyncio.to_thread(self._s3.get_object, Bucket=self._bucket, Key=key)
    return json.loads(response['Body'].read().decode('utf-8'))

  async def find_steps_by_uuid(self, uuids: set[str]) -> list[dict]:
    """Return universal row identities carrying any requested UUID."""
    if self._uuid_index is None:
      raise RuntimeError('UUID lookup requires a configured index')

    async def find(uuid: str) -> list[dict]:
      matches: list[dict] = []
      exclusive_start_key: Optional[dict] = None
      while True:
        kwargs: dict[str, Any] = {
          'TableName': self._steps_table,
          'IndexName': self._uuid_index,
          'KeyConditionExpression': '#uuid = :uuid',
          'ProjectionExpression': 'trail_id, step_id, #uuid',
          'ExpressionAttributeNames': {'#uuid': 'uuid'},
          'ExpressionAttributeValues': {':uuid': _ddb(uuid)},
        }
        if exclusive_start_key is not None:
          kwargs['ExclusiveStartKey'] = exclusive_start_key
        response = await asyncio.to_thread(self._dynamo.query, **kwargs)
        for raw in response.get('Items', []):
          row = _from_ddb_item(raw)
          if (
            row is None
            or not isinstance(row.get('trail_id'), str)
            or not isinstance(row.get('step_id'), int)
            or isinstance(row.get('step_id'), bool)
            or row.get('uuid') != uuid
          ):
            raise ValueError(f'UUID index returned malformed row: {row!r}')
          matches.append({'trail_id': row['trail_id'], 'step_id': row['step_id'], 'uuid': uuid})
        exclusive_start_key = response.get('LastEvaluatedKey')
        if exclusive_start_key is None:
          return matches

    pages = await asyncio.gather(*(find(uuid) for uuid in sorted(uuids)))
    return sorted(
      (match for page in pages for match in page),
      key=lambda row: (row['trail_id'], row['step_id']),
    )

  async def get_step(self, trail_id: str, step_id: int) -> Optional[dict]:
    header = await self._required_universal_header(trail_id)
    response = await asyncio.to_thread(
      self._dynamo.get_item,
      TableName=self._steps_table,
      Key=_ddb_item({'trail_id': trail_id, 'step_id': step_id}),
      ConsistentRead=True,
    )
    row = _from_ddb_item(response.get('Item'))
    if row is None:
      return None
    return await self._materialize_row(header['harness'], row, resolve_large=True)

  async def query_step_uuids(self, trail_id: str, *, through: Optional[int]) -> list[dict]:
    await self._required_universal_header(trail_id)
    if through is not None and through < 0:
      return []
    expression_values = {':trail_id': _ddb(trail_id)}
    key_condition = 'trail_id = :trail_id'
    if through is not None:
      expression_values[':through'] = _ddb(through)
      key_condition += ' AND step_id <= :through'
    rows: list[dict] = []
    exclusive_start_key: Optional[dict] = None
    while True:
      kwargs: dict[str, Any] = {
        'TableName': self._steps_table,
        'KeyConditionExpression': key_condition,
        'ProjectionExpression': 'trail_id, step_id, #uuid',
        'ExpressionAttributeNames': {'#uuid': 'uuid'},
        'ExpressionAttributeValues': expression_values,
      }
      if exclusive_start_key is not None:
        kwargs['ExclusiveStartKey'] = exclusive_start_key
      response = await asyncio.to_thread(self._dynamo.query, **kwargs)
      for raw in response.get('Items', []):
        row = _from_ddb_item(raw)
        if row is not None and isinstance(row.get('uuid'), str):
          rows.append({'step_id': row['step_id'], 'uuid': row['uuid']})
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return rows

  async def query_steps(self, trail_id: str, *, after: Optional[int], limit: int) -> dict:
    header = await self._required_universal_header(trail_id)
    return await self._query_universal_rows(
      header,
      after=after,
      limit=limit,
      resolve_large=header['harness'] == 'claude',
    )

  async def query_messages(
    self,
    trail_id: str,
    *,
    after: Optional[int],
    limit: int,
    types: Optional[set[str]],
  ) -> dict:
    header = await self._required_universal_header(trail_id)
    adapter = self._backend(header['harness'])
    page = await self._query_universal_rows(header, after=after, limit=limit, resolve_large=True)
    messages = [message for record in page['steps'] for message in adapter.project(record)]
    undeclared = {message['type'] for message in messages} - adapter.emitted_message_types
    if len(undeclared) > 0:
      raise RuntimeError(f'adapter emitted undeclared message types: {sorted(undeclared)}')
    if types is not None:
      messages = [message for message in messages if message['type'] in types]
    return {'messages': messages, 'next': page['next']}

  async def _query_universal_rows(
    self,
    header: dict,
    *,
    after: Optional[int],
    limit: int,
    resolve_large: bool,
  ) -> dict:
    kwargs: dict[str, Any] = {
      'TableName': self._steps_table,
      'KeyConditionExpression': 'trail_id = :trail_id',
      'ExpressionAttributeValues': {':trail_id': _ddb(header['id'])},
      'Limit': limit,
    }
    if after is not None:
      kwargs['ExclusiveStartKey'] = _ddb_item({'trail_id': header['id'], 'step_id': after})
    response = await asyncio.to_thread(self._dynamo.query, **kwargs)
    raw_rows = [
      row for item in response.get('Items', []) if (row := _from_ddb_item(item)) is not None
    ]
    rows = await asyncio.gather(
      *(
        self._materialize_row(header['harness'], row, resolve_large=resolve_large)
        for row in raw_rows
      )
    )
    last = response.get('LastEvaluatedKey')
    next_cursor = _from_ddb(last['step_id']) if last is not None else None
    return {'steps': rows, 'next': next_cursor}

  async def _materialize_row(self, harness: str, row: dict, *, resolve_large: bool) -> dict:
    resolved = await self._resolve_body(
      dict(row), resolve_large=resolve_large, parse_json=harness != 'claude'
    )
    if harness == 'claude':
      resolved.update(self._backend(harness).parse(resolved).native)
    return resolved

  async def _resolve_body(
    self, item: dict, *, resolve_large: bool, parse_json: bool = True
  ) -> dict:
    key = item.pop('body_s3', None)
    encoding = item.pop('body_encoding', None)
    if key is None:
      return item
    head = await asyncio.to_thread(self._s3.head_object, Bucket=self._bucket, Key=key)
    size = int(head.get('ContentLength', 0))
    if resolve_large or size <= storage_types.INLINE_RESPONSE_THRESHOLD_BYTES:
      stored = await asyncio.to_thread(self._s3.get_object, Bucket=self._bucket, Key=key)
      raw = stored['Body'].read()
      if encoding == 'text' or (encoding is None and not parse_json):
        item['body'] = raw.decode('utf-8')
      elif encoding == 'json':
        item['body'] = json.loads(raw)
      elif encoding is None:
        try:
          item['body'] = json.loads(raw)
        except json.JSONDecodeError:
          item['body'] = raw.decode('utf-8')
      else:
        raise ValueError(f'unsupported body encoding: {encoding}')
    else:
      url = await asyncio.to_thread(
        self._s3.generate_presigned_url,
        ClientMethod='get_object',
        Params={'Bucket': self._bucket, 'Key': key},
        ExpiresIn=storage_types.PRESIGNED_URL_TTL_SECONDS,
      )
      item['body'] = {'s3': key, 'url': url, 'size': size}
    return item

  async def recompute(self, trail_id: str) -> dict:
    return await self._operations.recompute(trail_id)

  async def check(self, trail_id: Optional[str] = None) -> dict:
    return await self._operations.check(trail_id)

  async def relink(self, trail_id: str, forked_from: dict, delete_count: int) -> dict:
    return await self._operations.relink(trail_id, forked_from, delete_count)

  async def list_trails(
    self,
    *,
    harness: Optional[str],
    bro: Optional[str],
    forked_from: Optional[str],
    since: Optional[str],
    until: Optional[str],
    cursor: Optional[str],
    limit: int,
    project: bool = True,
  ) -> dict:
    selected = [value is not None for value in (harness, bro, forked_from)]
    if sum(selected) > 1:
      raise ValueError('only one of harness/bro/forked_from may be set')
    if harness is not None:
      index, partition_name, partition_value = 'harness-started_at-index', 'harness', harness
    elif bro is not None:
      index, partition_name, partition_value = 'bro-started_at-index', 'bro', bro
    elif forked_from is not None:
      index, partition_name, partition_value = (
        'forked-from-id-index',
        'forked_from_id',
        forked_from,
      )
    else:
      index, partition_name, partition_value = 'all-index', GSI_PK_ATTRIBUTE, GSI_PK_VALUE
    response = await asyncio.to_thread(
      self._dynamo.query,
      **_range_query(
        table=self._trails_table,
        index=index,
        partition_name=partition_name,
        partition_value=partition_value,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
      ),
    )
    items = [item for raw in response.get('Items', []) if (item := _from_ddb_item(raw)) is not None]
    if project:
      items = [self._project_header(item) for item in items]
    last = response.get('LastEvaluatedKey')
    next_cursor = json.dumps(_from_ddb_item(last)) if last is not None else None
    return {'trails': items, 'next': next_cursor}


def _format_iso(moment: datetime) -> str:
  return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _now_iso() -> str:
  return storage_types.now_iso()


def _range_query(
  *,
  table: str,
  index: str,
  partition_name: str,
  partition_value: str,
  since: Optional[str],
  until: Optional[str],
  limit: int,
  cursor: Optional[str],
) -> dict:
  values: dict = {':partition': _ddb(partition_value)}
  if since is not None and until is not None:
    condition = f'{partition_name} = :partition AND started_at BETWEEN :since AND :until'
    values[':since'] = _ddb(since)
    values[':until'] = _ddb(until)
  elif since is not None:
    condition = f'{partition_name} = :partition AND started_at >= :since'
    values[':since'] = _ddb(since)
  elif until is not None:
    condition = f'{partition_name} = :partition AND started_at <= :until'
    values[':until'] = _ddb(until)
  else:
    condition = f'{partition_name} = :partition'
  kwargs: dict = {
    'TableName': table,
    'IndexName': index,
    'KeyConditionExpression': condition,
    'ExpressionAttributeValues': values,
    'ScanIndexForward': False,
    'Limit': limit,
  }
  if cursor is not None:
    kwargs['ExclusiveStartKey'] = _ddb_item(json.loads(cursor))
  return kwargs
