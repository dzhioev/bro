"""DynamoDB/S3 trails store and maintenance surface."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import boto3

from bro.trails import backends, rows
from bro.trails.model import (
  UNREPORTED_END_INFERENCE,
  BlazeRequest,
  canonical_json_bytes,
  payload_sha256,
  validate_end,
)
from bro.trails.rows import AggregateState, state_fields
from bro.trails.server import dynamo_types
from bro.trails.server.operations import Operations
from bro.trails.store import AppendConflict, TrailNotFound, TrailsStore

SPILLOVER_THRESHOLD_BYTES = dynamo_types.SPILLOVER_THRESHOLD_BYTES
MAX_BODY_BYTES = dynamo_types.MAX_BODY_BYTES
BodyTooLarge = dynamo_types.BodyTooLarge

GSI_PK_ATTRIBUTE = 'gsi_pk'
GSI_PK_VALUE = 'trail'
UNREPORTED_AFTER_SECONDS = 3600
SWEEP_WINDOW_DAYS = 30
_ddb = dynamo_types.ddb
_ddb_item = dynamo_types.ddb_item
_from_ddb = dynamo_types.from_ddb
_from_ddb_item = dynamo_types.from_ddb_item


class DynamoStore(TrailsStore):
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
    self._executor = ThreadPoolExecutor(thread_name_prefix='trails-dynamo')
    self._operations = Operations(
      dynamo=dynamo,
      s3=s3,
      trails_table=trails_table,
      steps_table=self._steps_table,
      bucket=bucket,
      backend=self._backend,
      required_header=self._required_header,
      resolve_row_body=self._resolve_row_body,
    )

  def _backend(self, harness: str) -> backends.Adapter:
    try:
      return self._backends[harness]
    except KeyError as exception:
      raise ValueError(f'unsupported harness: {harness}') from exception

  def _prepare_rows(
    self,
    *,
    trail_id: str,
    offset: int,
    payloads: list[Any],
    adapter: backends.Adapter,
    default_timestamp: str,
    state: rows.AggregateState,
    seen_billing_keys: set[str],
  ) -> list[dict]:
    prepared = rows.build_rows(
      trail_id=trail_id,
      offset=offset,
      payloads=payloads,
      adapter=adapter,
      default_timestamp=default_timestamp,
      state=state,
      seen_billing_keys=seen_billing_keys,
    )
    for row in prepared:
      body = row.pop('body')
      body_payload = dynamo_types.body_bytes(body)
      if len(body_payload) > dynamo_types.MAX_BODY_BYTES:
        raise BodyTooLarge(f'body size {len(body_payload)} exceeds {dynamo_types.MAX_BODY_BYTES}')
      if len(body_payload) < dynamo_types.SPILLOVER_THRESHOLD_BYTES:
        row['body'] = body
        continue
      key = dynamo_types.universal_spillover_key(trail_id, row['step_id'], body_payload)
      self._s3.put_object(
        Bucket=self._bucket,
        Key=key,
        Body=body_payload,
        ContentType='application/json',
      )
      row['body_s3'] = key
      row['body_encoding'] = 'text' if isinstance(body, str) else 'json'
    return prepared

  def blaze(self, request: BlazeRequest) -> dict:
    adapter = self._backend(request.harness)
    adapter.validate_create(request.native)
    if request.harness == 'bro' and request.bro is None:
      raise ValueError('bro is required for the bro harness')
    if request.attempt_key is not None:
      recorded = self._recorded_attempt(request.attempt_key)
      if recorded is not None:
        return recorded
    decision = None
    forked_from = request.forked_from
    if request.lineage is not None:
      decision = backends.resolve_lineage(adapter, request, self)
      if not decision.adopt:
        return {'adopted': False, 'reason': decision.reason}
      forked_from = decision.forked_from
    trail_id = dynamo_types.new_id()
    started_at = _now_iso()
    launch_context = request.body.get('launch_context')
    opened = adapter.open(request.body)
    if len(opened.records) > dynamo_types.MAX_TRANSACTION_RECORDS:
      raise ValueError(
        f'a trail may open with at most {dynamo_types.MAX_TRANSACTION_RECORDS} records'
      )
    if launch_context is not None:
      self._store_context(trail_id, launch_context)

    item: dict[str, Any] = {
      'id': trail_id,
      'harness': request.harness,
      'version': request.version,
      'started_at': started_at,
      'end': None,
      'last_alive_at': started_at,
      'interactive': request.interactive,
      'surface': request.surface,
      'turn_count': 0,
      'native': dict(request.native),
      GSI_PK_ATTRIBUTE: GSI_PK_VALUE,
    }
    optional = {
      'bro': request.bro,
      'hold': request.hold,
      'forked_from': forked_from,
      'summoned_by': request.summoned_by,
      'subject': request.subject,
      'location': request.location,
      'context_s3': dynamo_types.context_key(trail_id) if launch_context is not None else None,
    }
    item.update({key: value for key, value in optional.items() if value is not None})
    if forked_from is not None:
      item['forked_from_id'] = forked_from['trail_id']

    item['body_storage'] = dynamo_types.UNIVERSAL_BODY_STORAGE
    item['extent'] = 0
    state = AggregateState(item)
    seen_billing_keys: set[str] = set()
    rows = self._prepare_rows(
      trail_id=trail_id,
      offset=0,
      payloads=opened.records,
      adapter=adapter,
      default_timestamp=started_at,
      state=state,
      seen_billing_keys=seen_billing_keys,
    )
    item.update(state_fields(state, len(rows)))
    result = backends.blaze_result(trail_id, started_at, decision)
    transaction_items: list[dict] = [
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
    ]
    if request.attempt_key is not None:
      transaction_items.append(
        {
          'Put': {
            'TableName': self._trails_table,
            'Item': _ddb_item({'id': _attempt_id(request.attempt_key), 'result': result}),
            'ConditionExpression': 'attribute_not_exists(id)',
          }
        }
      )
    try:
      self._dynamo.transact_write_items(TransactItems=transaction_items)
    except self._dynamo.exceptions.TransactionCanceledException:
      if request.attempt_key is None:
        raise
      recorded = self._recorded_attempt(request.attempt_key)
      if recorded is None:
        raise
      return recorded
    return result

  def _recorded_attempt(self, attempt_key: str) -> Optional[dict]:
    """the blaze result an earlier attempt under this key recorded, or None when
    the key has opened no trail."""
    response = self._dynamo.get_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': _attempt_id(attempt_key)}),
      ConsistentRead=True,
    )
    item = _from_ddb_item(response.get('Item'))
    return None if item is None else item['result']

  def _store_context(self, trail_id: str, context: Any) -> None:
    self._s3.put_object(
      Bucket=self._bucket,
      Key=dynamo_types.context_key(trail_id),
      Body=json.dumps(context, ensure_ascii=False).encode('utf-8'),
      ContentType='application/json',
    )

  def append_records(
    self,
    trail_id: str,
    offset: int,
    records: list[Any],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    if offset < 0:
      raise ValueError('offset must be non-negative')
    header = self._required_universal_header(trail_id)
    actual = self._header_extent(header)
    expected_end = offset + len(records)
    if actual != offset:
      if actual == expected_end and self._batch_matches(trail_id, offset, records):
        return {'extent': actual, 'appended': 0, 'duplicate': True}
      raise AppendConflict(offset, actual)
    self._store_tools(tools if tools is not None else {})
    if len(records) == 0:
      return {'extent': actual, 'appended': 0}

    adapter = self._backend(header['harness'])
    state = AggregateState(header)
    seen_billing_keys: set[str] = set()
    committed = 0
    while committed < len(records):
      chunk = records[committed : committed + dynamo_types.MAX_TRANSACTION_RECORDS]
      chunk_offset = offset + committed
      rows = self._prepare_rows(
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
        self._dynamo.transact_write_items(
          TransactItems=transaction_items,
        )
      except self._dynamo.exceptions.TransactionCanceledException as exception:
        refreshed = self._required_header(trail_id)
        refreshed_extent = self._header_extent(refreshed)
        if (
          committed == 0
          and refreshed_extent == expected_end
          and self._batch_matches(trail_id, offset, records)
        ):
          return {'extent': refreshed_extent, 'appended': 0, 'duplicate': True}
        if refreshed_extent != chunk_offset:
          raise AppendConflict(chunk_offset, refreshed_extent) from exception
        raise RuntimeError(
          f'ordinal {chunk_offset} is already occupied at the trail extent'
        ) from exception
      committed += len(rows)
    return {'extent': offset + committed, 'appended': committed}

  def _batch_matches(self, trail_id: str, offset: int, records: list[Any]) -> bool:
    response = self._dynamo.query(
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
    expected = [payload_sha256(record) for record in records]
    return len(rows) == len(records) and all(
      row.get('payload_sha256') == sha256 for row, sha256 in zip(rows, expected, strict=True)
    )

  def _store_tools(self, tools: dict[str, Any]) -> None:
    if not isinstance(tools, dict):
      raise ValueError('tools must be an object keyed by sha256')
    for sha256, body in tools.items():
      if not isinstance(sha256, str) or len(sha256) != 64:
        raise ValueError('tool blob keys must be sha256 hex strings')
      payload = canonical_json_bytes(body)
      if dynamo_types.sha256_hex(payload) != sha256:
        raise ValueError(f'tool blob hash mismatch: {sha256}')
      if sha256 in self._stored_tool_hashes:
        continue
      self._s3.put_object(
        Bucket=self._bucket,
        Key=dynamo_types.tool_blob_key(sha256),
        Body=payload,
        ContentType='application/json',
      )
      self._stored_tool_hashes.add(sha256)

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
      ':storage': _ddb(dynamo_types.UNIVERSAL_BODY_STORAGE),
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

  def set_subject(self, trail_id: str, subject: Optional[str]) -> dict:
    self._required_header(trail_id)
    self._dynamo.update_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConditionExpression='attribute_exists(id)',
      UpdateExpression='SET subject = :subject',
      ExpressionAttributeValues={':subject': _ddb(subject)},
    )
    return self._project_header(self._required_header(trail_id))

  def end_trail(
    self,
    trail_id: str,
    reason: str,
    detail: Optional[str] = None,
  ) -> None:
    validate_end(reason, detail)
    self._required_header(trail_id)
    timestamp = _now_iso()
    end = {'at': timestamp, 'reason': reason}
    if detail is not None:
      end['detail'] = detail
    self._dynamo.update_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConditionExpression='attribute_exists(id)',
      UpdateExpression='SET #end = :end, last_alive_at = :timestamp',
      ExpressionAttributeNames={'#end': 'end'},
      ExpressionAttributeValues={':end': _ddb(end), ':timestamp': _ddb(timestamp)},
    )

  def keepalive(self, trail_id: str) -> None:
    timestamp = _now_iso()
    try:
      self._dynamo.update_item(
        TableName=self._trails_table,
        Key=_ddb_item({'id': trail_id}),
        ConditionExpression='attribute_exists(id)',
        UpdateExpression='SET last_alive_at = :timestamp',
        ExpressionAttributeValues={':timestamp': _ddb(timestamp)},
      )
    except self._dynamo.exceptions.ConditionalCheckFailedException as exception:
      raise TrailNotFound(trail_id) from exception

  def sweep_unreported(self) -> list[str]:
    now = datetime.now(UTC)
    cutoff = _format_iso(now - timedelta(seconds=UNREPORTED_AFTER_SECONDS))
    since = _format_iso(now - timedelta(days=SWEEP_WINDOW_DAYS))
    swept: list[str] = []
    cursor: Optional[str] = None
    while True:
      page = self._list_trails(
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
        if self._stamp_unreported(item['id'], item['last_alive_at']):
          swept.append(item['id'])
      cursor = page['next']
      if cursor is None:
        return swept

  def _stamp_unreported(self, trail_id: str, ended_at: str) -> bool:
    try:
      self._dynamo.update_item(
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

  def get_trail(self, trail_id: str) -> dict:
    return self._project_header(self._required_header(trail_id))

  def _required_header(self, trail_id: str) -> dict:
    response = self._dynamo.get_item(
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConsistentRead=True,
    )
    item = _from_ddb_item(response.get('Item'))
    if item is None:
      raise TrailNotFound(trail_id)
    return item

  def _required_universal_header(self, trail_id: str) -> dict:
    header = self._required_header(trail_id)
    if header.get('body_storage') != dynamo_types.UNIVERSAL_BODY_STORAGE:
      raise ValueError(f'trail {trail_id} has no body in {dynamo_types.UNIVERSAL_BODY_STORAGE}')
    return header

  def _project_header(self, item: dict) -> dict:
    raw_usage = item.get('native', {}).get('usage', {})
    if not isinstance(raw_usage, dict):
      raise ValueError('native.usage must be an object')
    return {**item, 'usage': raw_usage, 'models': sorted(raw_usage)}

  def get_launch_context(self, trail_id: str) -> Optional[Any]:
    header = self._required_header(trail_id)
    key = header.get('context_s3')
    if key is None:
      key = header.get('native', {}).get('context_s3')
    if key is None:
      return None
    response = self._s3.get_object(Bucket=self._bucket, Key=key)
    return json.loads(response['Body'].read().decode('utf-8'))

  def find_segment_steps(self, segments: set[str], uuids: set[str]) -> list[dict]:
    """Return row identities carrying any requested UUID, restricted to the
    trails recording one of `segments`."""
    if len(uuids) == 0 or len(segments) == 0:
      return []
    if self._uuid_index is None:
      raise RuntimeError('UUID lookup requires a configured index')

    def find(uuid: str) -> list[dict]:
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
        response = self._dynamo.query(**kwargs)
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

    pages = self._executor.map(find, sorted(uuids))
    matches = [match for page in pages for match in page]
    headers: dict[str, dict] = {}
    kept: list[dict] = []
    for match in matches:
      trail_id = match['trail_id']
      if trail_id not in headers:
        headers[trail_id] = self.get_trail(trail_id)
      header = headers[trail_id]
      if header.get('native', {}).get('segment') in segments:
        kept.append({**match, 'header': header})
    return sorted(kept, key=lambda row: (row['trail_id'], row['step_id']))

  def get_step(self, trail_id: str, step_id: int) -> dict:
    header = self._required_universal_header(trail_id)
    if step_id < 0:
      raise TrailNotFound(f'{trail_id}/{step_id}')
    response = self._dynamo.get_item(
      TableName=self._steps_table,
      Key=_ddb_item({'trail_id': trail_id, 'step_id': step_id}),
      ConsistentRead=True,
    )
    row = _from_ddb_item(response.get('Item'))
    if row is None:
      raise TrailNotFound(f'{trail_id}/{step_id}')
    return self._resolve_row_body(header['harness'], row)

  def step_payload_hashes(self, trail_id: str, step_ids: list[int]) -> dict[int, str]:
    self._required_universal_header(trail_id)
    hashes: dict[int, str] = {}
    for step_id in sorted({step_id for step_id in step_ids if step_id >= 0}):
      response = self._dynamo.get_item(
        TableName=self._steps_table,
        Key=_ddb_item({'trail_id': trail_id, 'step_id': step_id}),
        ProjectionExpression='payload_sha256',
        ConsistentRead=True,
      )
      row = _from_ddb_item(response.get('Item'))
      digest = row.get('payload_sha256') if row is not None else None
      if isinstance(digest, str):
        hashes[step_id] = digest
    return hashes

  def get_step_uuids(self, trail_id: str, *, through: Optional[int] = None) -> list[dict]:
    self._required_universal_header(trail_id)
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
      response = self._dynamo.query(**kwargs)
      for raw in response.get('Items', []):
        row = _from_ddb_item(raw)
        if row is not None and isinstance(row.get('uuid'), str):
          rows.append({'step_id': row['step_id'], 'uuid': row['uuid']})
      exclusive_start_key = response.get('LastEvaluatedKey')
      if exclusive_start_key is None:
        return rows

  def get_steps(
    self,
    trail_id: str,
    *,
    after: Optional[int] = None,
    limit: Optional[int] = None,
  ) -> dict:
    page_size = 100 if limit is None else limit
    if page_size < 1 or page_size > 500:
      raise ValueError('limit must be between 1 and 500')
    header = self._required_universal_header(trail_id)
    return self._query_universal_rows(header, after=after, limit=page_size)

  def get_messages(
    self,
    trail_id: str,
    *,
    types: Optional[set[str]] = None,
    after: Optional[int] = None,
    limit: Optional[int] = None,
  ) -> dict:
    page_size = 100 if limit is None else limit
    if page_size < 1 or page_size > 500:
      raise ValueError('limit must be between 1 and 500')
    header = self._required_universal_header(trail_id)
    adapter = self._backend(header['harness'])
    page = self._query_universal_rows(header, after=after, limit=page_size)
    messages = rows.project_messages(adapter, page['steps'], types)
    return {'messages': messages, 'next': page['next']}

  def _query_universal_rows(
    self,
    header: dict,
    *,
    after: Optional[int],
    limit: int,
  ) -> dict:
    kwargs: dict[str, Any] = {
      'TableName': self._steps_table,
      'KeyConditionExpression': 'trail_id = :trail_id',
      'ExpressionAttributeValues': {':trail_id': _ddb(header['id'])},
      'Limit': limit,
    }
    if after is not None:
      kwargs['ExclusiveStartKey'] = _ddb_item({'trail_id': header['id'], 'step_id': after})
    response = self._dynamo.query(**kwargs)
    raw_rows = [
      row for item in response.get('Items', []) if (row := _from_ddb_item(item)) is not None
    ]
    rows = self._executor.map(
      lambda row: self._resolve_row_body(header['harness'], row),
      raw_rows,
    )
    last = response.get('LastEvaluatedKey')
    next_cursor = _from_ddb(last['step_id']) if last is not None else None
    return {'steps': list(rows), 'next': next_cursor}

  def _resolve_row_body(self, harness: str, row: dict) -> dict:
    return self._resolve_body(dict(row), parse_json=harness != 'claude')

  def _resolve_body(self, item: dict, *, parse_json: bool = True) -> dict:
    key = item.pop('body_s3', None)
    encoding = item.pop('body_encoding', None)
    if key is None:
      return item
    stored = self._s3.get_object(Bucket=self._bucket, Key=key)
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
    return item

  def recompute(self, trail_id: str) -> dict:
    return self._operations.recompute(trail_id)

  def check(self, trail_id: Optional[str] = None) -> dict:
    return self._operations.check(trail_id)

  def relink(self, trail_id: str, forked_from: dict, delete_count: int) -> dict:
    return self._operations.relink(trail_id, forked_from, delete_count)

  def close(self) -> None:
    self._executor.shutdown()

  def list_trails(
    self,
    *,
    harness: Optional[str] = None,
    bro: Optional[str] = None,
    forked_from: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
  ) -> dict:
    page_size = 100 if limit is None else limit
    if page_size < 1 or page_size > 100:
      raise ValueError('limit must be between 1 and 100')
    return self._list_trails(
      harness=harness,
      bro=bro,
      forked_from=forked_from,
      since=since,
      until=until,
      cursor=cursor,
      limit=page_size,
      project=True,
    )

  def _list_trails(
    self,
    *,
    harness: Optional[str],
    bro: Optional[str],
    forked_from: Optional[str],
    since: Optional[str],
    until: Optional[str],
    cursor: Optional[str],
    limit: int,
    project: bool,
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
    response = self._dynamo.query(
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


def build_dynamo_store(config: dict[str, Any]) -> DynamoStore:
  fields = {'backend', 'trails_table', 'steps_table', 'uuid_index', 'bucket', 'region'}
  unknown = set(config) - fields
  if len(unknown) > 0:
    raise ValueError(f'unknown dynamo trails fields: {sorted(unknown)}')
  required = fields - {'backend'}
  missing = required - set(config)
  if len(missing) > 0:
    raise ValueError(f'dynamo trails config is missing fields: {sorted(missing)}')
  for field in sorted(required):
    value = config[field]
    if not isinstance(value, str) or len(value) == 0:
      raise ValueError(f'dynamo trails {field} must be a non-empty string')
  session = boto3.Session(region_name=config['region'])
  return DynamoStore(
    dynamo=session.client('dynamodb'),
    s3=session.client('s3'),
    trails_table=config['trails_table'],
    steps_table=config['steps_table'],
    uuid_index=config['uuid_index'],
    bucket=config['bucket'],
  )


def _attempt_id(attempt_key: str) -> str:
  """the trails-table id an attempt key's answer is recorded under; the `#` is
  what keeps those ids out of the space trails are minted in."""
  return f'attempt#{attempt_key}'


def _format_iso(moment: datetime) -> str:
  return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _now_iso() -> str:
  return dynamo_types.now_iso()


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
