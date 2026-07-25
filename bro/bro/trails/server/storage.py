"""Universal trail-header registry and harness body-backend dispatch."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from trails.server import storage_types
from trails.server.backends import BodyBackend, BroBackend, ClaudeBackend

SPILLOVER_THRESHOLD_BYTES = storage_types.SPILLOVER_THRESHOLD_BYTES
MAX_BODY_BYTES = storage_types.MAX_BODY_BYTES
INLINE_RESPONSE_THRESHOLD_BYTES = storage_types.INLINE_RESPONSE_THRESHOLD_BYTES
PRESIGNED_URL_TTL_SECONDS = storage_types.PRESIGNED_URL_TTL_SECONDS
STEP_KINDS = storage_types.BRO_STEP_KINDS
MESSAGE_TYPES = storage_types.MESSAGE_TYPES
TrailNotFound = storage_types.TrailNotFound
BodyTooLarge = storage_types.BodyTooLarge

GSI_PK_ATTRIBUTE = 'gsi_pk'
GSI_PK_VALUE = 'trail'
LOST_AFTER_SECONDS = 3600
SWEEP_WINDOW_DAYS = 30

_ddb = storage_types.ddb
_ddb_item = storage_types.ddb_item
_from_ddb = storage_types.from_ddb
_from_ddb_item = storage_types.from_ddb_item
_body_size_bytes = storage_types.body_size_bytes


def _format_iso(moment: datetime) -> str:
  return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _now_iso() -> str:
  return storage_types.now_iso()


class Storage:
  def __init__(self, *, dynamo, s3, trails_table: str, steps_table: str, bucket: str):
    self._dynamo = dynamo
    self._s3 = s3
    self._trails_table = trails_table
    self._steps_table = steps_table
    self._bucket = bucket
    self._backends: dict[str, BodyBackend] = {}

  def _backend(self, harness: str) -> BodyBackend:
    backend = self._backends.get(harness)
    if backend is not None:
      return backend
    if harness == 'bro':
      backend = BroBackend(
        dynamo=self._dynamo,
        s3=self._s3,
        trails_table=self._trails_table,
        steps_table=self._steps_table,
        bucket=self._bucket,
      )
    elif harness == 'claude':
      backend = ClaudeBackend(s3=self._s3, bucket=self._bucket)
    else:
      raise ValueError(f'unsupported harness: {harness}')
    self._backends[harness] = backend
    return backend

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
    trail_id = trail_id if trail_id is not None else storage_types.new_id()
    started_at = _now_iso()
    backend = self._backend(harness)
    opened_body = await backend.open(
      trail_id, body, native if native is not None else {}, started_at
    )
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
      'native': opened_body.native,
      GSI_PK_ATTRIBUTE: GSI_PK_VALUE,
    }
    optional = {
      'bro': bro,
      'hold': hold,
      'forked_from': forked_from,
      'summoned_by': summoned_by,
      'subject': subject,
      'location': location,
    }
    item.update({key: value for key, value in optional.items() if value is not None})
    if forked_from is not None:
      item['forked_from_id'] = forked_from['trail_id']
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
        *opened_body.transaction_items,
      ],
    )
    return {'id': trail_id, 'started_at': started_at}

  async def put_step(
    self,
    *,
    trail_id: str,
    kind: str,
    body: Any,
    extras: dict,
    step_id: Optional[str] = None,
  ) -> dict:
    header = await self._required_header(trail_id)
    if header['harness'] != 'bro':
      raise ValueError('step append is available only for bro trails')
    return await self._backend('bro').replace_or_append_body(
      trail_id,
      body,
      append=True,
      metadata={'kind': kind, 'step_id': step_id, **extras},
    )

  async def replace_artifact(self, trail_id: str, artifact: str, metadata: dict) -> dict:
    header = await self._required_header(trail_id)
    backend = self._backend(header['harness'])
    if header['harness'] != 'claude':
      raise ValueError('artifact replacement is available only for claude trails')
    unknown = set(metadata) - {'harness_version', 'usage'}
    if len(unknown) > 0:
      raise ValueError(f'immutable or unknown native fields: {sorted(unknown)}')
    updates = await backend.replace_or_append_body(
      trail_id, artifact, append=False, metadata=metadata
    )
    await self.update_header(trail_id, {'native': updates}, allow_server_derived=True)
    return updates

  async def update_header(
    self, trail_id: str, changes: dict, *, allow_server_derived: bool = False
  ) -> dict:
    header = await self._required_header(trail_id)
    allowed = {'subject', 'last_alive_at', 'turn_count'}
    unknown = set(changes) - allowed - {'native'}
    if len(unknown) > 0:
      raise ValueError(f'immutable or unknown header fields: {sorted(unknown)}')
    native_changes = changes.get('native', {})
    if not isinstance(native_changes, dict):
      raise ValueError('native must be an object')
    allowed_native = {
      'bro': set(),
      'claude': {'harness_version', 'usage'},
    }[header['harness']]
    if allow_server_derived and header['harness'] == 'claude':
      allowed_native.update({'line_count', 'size_bytes'})
    unknown_native = set(native_changes) - allowed_native
    if len(unknown_native) > 0:
      raise ValueError(f'immutable or unknown native fields: {sorted(unknown_native)}')

    names: dict[str, str] = {}
    values: dict[str, dict] = {}
    assignments: list[str] = []
    for index, (key, value) in enumerate(
      [(key, value) for key, value in changes.items() if key != 'native']
    ):
      names[f'#field{index}'] = key
      values[f':value{index}'] = _ddb(value)
      assignments.append(f'#field{index} = :value{index}')
    start = len(assignments)
    for offset, (key, value) in enumerate(native_changes.items(), start=start):
      names[f'#field{offset}'] = key
      values[f':value{offset}'] = _ddb(value)
      assignments.append(f'native.#field{offset} = :value{offset}')
    if len(assignments) == 0:
      return header
    await asyncio.to_thread(
      self._dynamo.update_item,
      TableName=self._trails_table,
      Key=_ddb_item({'id': trail_id}),
      ConditionExpression='attribute_exists(id)',
      UpdateExpression='SET ' + ', '.join(assignments),
      ExpressionAttributeNames=names,
      ExpressionAttributeValues=values,
    )
    return await self._required_header(trail_id)

  async def end_trail(
    self,
    *,
    trail_id: str,
    reason: str,
    detail: Optional[str],
    step_id: Optional[str] = None,
  ) -> dict:
    header = await self._required_header(trail_id)
    timestamp = _now_iso()
    if header['harness'] == 'bro':
      result = await self._backend('bro').replace_or_append_body(
        trail_id,
        {'reason': reason, **({'detail': detail} if detail is not None else {})},
        append=True,
        metadata={'kind': 'end', 'step_id': step_id},
      )
      duplicate = result.get('duplicate') is True
    else:
      duplicate = False
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
    return {'ended_at': timestamp, **({'duplicate': True} if duplicate else {})}

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

  async def sweep_lost(self) -> list[str]:
    now = datetime.now(UTC)
    cutoff = _format_iso(now - timedelta(seconds=LOST_AFTER_SECONDS))
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
        if await self._stamp_lost(item['id'], item['last_alive_at']):
          swept.append(item['id'])
      cursor = page['next']
      if cursor is None:
        return swept

  async def _stamp_lost(self, trail_id: str, ended_at: str) -> bool:
    try:
      await asyncio.to_thread(
        self._dynamo.update_item,
        TableName=self._trails_table,
        Key=_ddb_item({'id': trail_id}),
        ConditionExpression='attribute_type(#end, :null_type)',
        UpdateExpression='SET #end = :end',
        ExpressionAttributeNames={'#end': 'end'},
        ExpressionAttributeValues={
          ':end': _ddb({'at': ended_at, 'reason': 'lost'}),
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

  def _project_header(self, item: dict) -> dict:
    derived = self._backend(item['harness']).derive_aggregates(item.get('native', {}))
    return {**item, **derived}

  async def get_launch_context(self, trail_id: str) -> Optional[Any]:
    """the trail's stored launch-context document, or None when it has none."""
    header = await self._required_header(trail_id)
    key = header.get('native', {}).get('context_s3')
    if key is None:
      return None
    backend = self._backend(header['harness'])
    assert isinstance(backend, ClaudeBackend)  # context_s3 is claude-native
    return await backend.read_context(key)

  async def query_steps(self, trail_id: str, *, after: Optional[str], limit: int) -> dict:
    header = await self._required_header(trail_id)
    return await self._backend(header['harness']).iterate_native_records(
      trail_id, after=after, limit=limit
    )

  async def query_messages(
    self,
    trail_id: str,
    *,
    after: Optional[str],
    limit: int,
    types: Optional[set[str]],
  ) -> dict:
    header = await self._required_header(trail_id)
    backend = self._backend(header['harness'])
    page = await backend.iterate_native_records(trail_id, after=after, limit=limit)
    messages = backend.project_messages(page['steps'])
    if types is not None:
      messages = [message for message in messages if message['type'] in types]
    return {'messages': messages, 'next': page['next']}

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
      index, partition_name, partition_value = (
        'harness-started_at-index',
        'harness',
        harness,
      )
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
