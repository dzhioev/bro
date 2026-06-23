"""DynamoDB + S3 storage for the trails server.

Two DynamoDB tables and one S3 bucket back every operation:
- `trails`: PK=`trail_id`. Header row, incrementally updated aggregates, a
  constant `gsi_pk` for the global newest-first GSI, sparse `parent_trail_id`
  for the parent GSI.
- `trail_steps`: PK=`trail_id`, SK=`step_id`. Append-only, ULID-keyed step rows.
- `cw-trails-{account}` bucket: spillover for step bodies ≥ `SPILLOVER_THRESHOLD_BYTES`.

`Storage` is an async facade — every method `await`s the blocking boto3 calls
through `asyncio.to_thread`, matching the focus-server pattern. The atomic
"step write + aggregate update" pair is a single `TransactWriteItems`. The
9-kind step counter map (`aggregates.step_counts_by_kind`) is initialised on
trail creation so per-step `SET` updates always hit existing fields.
"""

import asyncio
import decimal
import json
from datetime import datetime, timezone
from typing import Any, Optional

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from ulid import ULID

SPILLOVER_THRESHOLD_BYTES = 50 * 1024
MAX_BODY_BYTES = 10 * 1024 * 1024
# response-side cap: spilled bodies smaller than this are fetched from S3 and
# returned inline; larger ones come back as presigned URLs the client follows.
INLINE_RESPONSE_THRESHOLD_BYTES = 1 * 1024 * 1024
PRESIGNED_URL_TTL_SECONDS = 3600

STEP_KINDS = (
  'system_prompt',
  'user_input',
  'reasoning',
  'assistant',
  'tool_call',
  'tool_result',
  'llm_call',
  'error',
  'end',
)

# the no-filter list path can't sort or range a full-table scan by time, so every
# trail item carries this constant attribute and the `all-index` GSI
# keys on it — a query there returns all trails by started_at (global newest-first
# + since/until range). one logical partition suffices at this write volume; shard
# the constant value if write throughput ever grows.
GSI_PK_ATTR = 'gsi_pk'
GSI_PK_VALUE = 'trail'


class TrailNotFound(Exception):
  pass


class BodyTooLarge(Exception):
  pass


_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _now_iso() -> str:
  return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _new_id() -> str:
  return str(ULID())


def _normalise_float(value: Any) -> Any:
  # TypeSerializer rejects float (DynamoDB numbers are Decimal), but JSON
  # payloads carry floats — inline llm_call bodies (temperature, top_p, ...),
  # tool arguments. convert on the way in; _normalise_decimal is the inverse
  # on the way out.
  if isinstance(value, float):
    return decimal.Decimal(str(value))
  if isinstance(value, list):
    return [_normalise_float(v) for v in value]
  if isinstance(value, dict):
    return {k: _normalise_float(v) for k, v in value.items()}
  return value


def _ddb(value: Any) -> dict:
  return _serializer.serialize(_normalise_float(value))


def _ddb_item(item: dict) -> dict:
  return {k: _ddb(v) for k, v in item.items()}


def _from_ddb(value: dict) -> Any:
  out = _deserializer.deserialize(value)
  return _normalise_decimal(out)


def _normalise_decimal(value: Any) -> Any:
  # boto3's TypeDeserializer returns Decimal for any N attribute. JSON
  # serialisation barfs on Decimal — convert to int/float here so handlers can
  # return raw dicts straight to web.json_response.
  if isinstance(value, decimal.Decimal):
    return int(value) if value == value.to_integral_value() else float(value)
  if isinstance(value, list):
    return [_normalise_decimal(v) for v in value]
  if isinstance(value, dict):
    return {k: _normalise_decimal(v) for k, v in value.items()}
  return value


def _from_ddb_item(item: Optional[dict]) -> Optional[dict]:
  if item is None:
    return None
  return {k: _from_ddb(v) for k, v in item.items()}


def _body_size_bytes(body: Any) -> int:
  if body is None:
    return 0
  if isinstance(body, str):
    return len(body.encode('utf-8'))
  return len(json.dumps(body, ensure_ascii=False).encode('utf-8'))


def _spillover_key(trail_id: str, step_id: str) -> str:
  return f'trails/{trail_id}/steps/{step_id}.json'


def _empty_step_counts() -> dict:
  return {kind: 0 for kind in STEP_KINDS}


class Storage:
  def __init__(self, *, dynamo, s3, trails_table: str, steps_table: str, bucket: str):
    self._dynamo = dynamo
    self._s3 = s3
    self._trails_table = trails_table
    self._steps_table = steps_table
    self._bucket = bucket

  async def create_trail(
    self,
    *,
    bro: str,
    bro_version: int,
    llm_spec: dict,
    system_prompt: str,
    parent: Optional[dict],
    interactive: bool,
    entry_point: str,
  ) -> dict:
    trail_id = _new_id()
    step_id = _new_id()
    started_at = _now_iso()

    aggregates = {
      'turn_count': 0,
      'tool_call_count': 0,
      'tokens_in': 0,
      'tokens_out': 0,
      'tokens_reasoning': 0,
      'step_counts_by_kind': _empty_step_counts() | {'system_prompt': 1},
    }
    trail_item: dict = {
      'trail_id': trail_id,
      'bro': bro,
      'bro_version': bro_version,
      'llm_spec': llm_spec,
      'started_at': started_at,
      'ended_at': None,
      'end_reason': None,
      'interactive': interactive,
      'entry_point': entry_point,
      'parent': parent,
      'continuation': None,
      'aggregates': aggregates,
    }
    # constant PK for the all-index GSI (global newest-first list).
    trail_item[GSI_PK_ATTR] = GSI_PK_VALUE
    if parent is not None:
      # surface parent.trail_id as a top-level attribute so the sparse
      # parent-trail-id GSI picks this trail up. trails without a parent omit
      # the attribute entirely (NULL would still pull them into the index).
      trail_item['parent_trail_id'] = parent['trail_id']

    step_item = {
      'trail_id': trail_id,
      'step_id': step_id,
      'ts': started_at,
      'kind': 'system_prompt',
      'body': system_prompt,
      'turn_index': 0,
    }

    await asyncio.to_thread(
      self._dynamo.transact_write_items,
      TransactItems=[
        {'Put': {'TableName': self._trails_table, 'Item': _ddb_item(trail_item)}},
        {'Put': {'TableName': self._steps_table, 'Item': _ddb_item(step_item)}},
      ],
    )
    return {'trail_id': trail_id, 'started_at': started_at}

  async def put_step(
    self,
    *,
    trail_id: str,
    kind: str,
    body: Any,
    extras: dict,
    step_id: Optional[str] = None,
  ) -> dict:
    size_bytes = _body_size_bytes(body)
    if size_bytes > MAX_BODY_BYTES:
      raise BodyTooLarge(f'body size {size_bytes} exceeds {MAX_BODY_BYTES}')
    # the client mints the step_id and reuses it across retries; the conditional
    # Put below turns a retried POST into an idempotent no-op. older clients that
    # send no id fall back to a server-minted ULID (no dedup, but harmless — a
    # fresh id never collides).
    step_id = step_id if step_id is not None else _new_id()
    ts = _now_iso()

    spilled_key: Optional[str] = None
    if size_bytes >= SPILLOVER_THRESHOLD_BYTES:
      spilled_key = _spillover_key(trail_id, step_id)
      payload = body if isinstance(body, (bytes, str)) else json.dumps(body, ensure_ascii=False)
      if isinstance(payload, str):
        payload = payload.encode('utf-8')
      await asyncio.to_thread(
        self._s3.put_object,
        Bucket=self._bucket,
        Key=spilled_key,
        Body=payload,
        ContentType='application/json',
      )

    step_item = {
      'trail_id': trail_id,
      'step_id': step_id,
      'ts': ts,
      'kind': kind,
      **extras,
    }
    # the spill pointer lives in its own sparse attribute, never inside `body` —
    # so a genuine body that happens to equal {'s3': key} can't read as spilled.
    if spilled_key is not None:
      step_item['body_s3'] = spilled_key
    else:
      step_item['body'] = body

    delta_turn = 1 if kind == 'llm_call' else 0
    delta_tool_call = 1 if kind == 'tool_call' else 0
    if kind == 'llm_call':
      delta_tokens_in = int(extras.get('tokens_in', 0))
      delta_tokens_out = int(extras.get('tokens_out', 0))
      delta_tokens_reasoning = int(extras.get('tokens_reasoning', 0))
    else:
      delta_tokens_in = 0
      delta_tokens_out = 0
      delta_tokens_reasoning = 0

    update = {
      'TableName': self._trails_table,
      'Key': _ddb_item({'trail_id': trail_id}),
      'ConditionExpression': 'attribute_exists(trail_id)',
      'UpdateExpression': (
        'SET aggregates.step_counts_by_kind.#k = aggregates.step_counts_by_kind.#k + :one, '
        'aggregates.turn_count = aggregates.turn_count + :d_turn, '
        'aggregates.tool_call_count = aggregates.tool_call_count + :d_tc, '
        'aggregates.tokens_in = aggregates.tokens_in + :d_in, '
        'aggregates.tokens_out = aggregates.tokens_out + :d_out, '
        'aggregates.tokens_reasoning = aggregates.tokens_reasoning + :d_reason'
      ),
      'ExpressionAttributeNames': {'#k': kind},
      'ExpressionAttributeValues': {
        ':one': _ddb(1),
        ':d_turn': _ddb(delta_turn),
        ':d_tc': _ddb(delta_tool_call),
        ':d_in': _ddb(delta_tokens_in),
        ':d_out': _ddb(delta_tokens_out),
        ':d_reason': _ddb(delta_tokens_reasoning),
      },
    }

    try:
      await asyncio.to_thread(
        self._dynamo.transact_write_items,
        TransactItems=[
          {
            'Put': {
              'TableName': self._steps_table,
              'Item': _ddb_item(step_item),
              'ConditionExpression': 'attribute_not_exists(step_id)',
            }
          },
          {'Update': update},
        ],
      )
    except self._dynamo.exceptions.TransactionCanceledException as e:
      # item 0 is the step Put: a failed attribute_not_exists means this step_id
      # already landed (a retried POST) — the transaction cancelled atomically,
      # so the aggregate was not double-incremented. report idempotent success.
      codes = _cancellation_codes(e)
      if len(codes) > 0 and codes[0] == 'ConditionalCheckFailed':
        return {'step_id': step_id, 'ts': ts, 'duplicate': True}
      # item 1 is the trail Update: a failed attribute_exists means no such trail.
      if _conditional_check_failed(e):
        raise TrailNotFound(trail_id) from e
      raise
    return {'step_id': step_id, 'ts': ts}

  async def end_trail(
    self,
    *,
    trail_id: str,
    reason: str,
    continuation: Optional[dict],
    step_id: Optional[str] = None,
  ) -> dict:
    step_id = step_id if step_id is not None else _new_id()
    ts = _now_iso()
    step_item = {
      'trail_id': trail_id,
      'step_id': step_id,
      'ts': ts,
      'kind': 'end',
      'body': {'reason': reason},
    }

    update_expr = (
      'SET ended_at = :ts, end_reason = :reason, '
      'aggregates.step_counts_by_kind.#k = aggregates.step_counts_by_kind.#k + :one'
    )
    expr_values: dict = {
      ':ts': _ddb(ts),
      ':reason': _ddb(reason),
      ':one': _ddb(1),
    }
    if continuation is not None:
      update_expr += ', continuation = :cont'
      expr_values[':cont'] = _ddb(continuation)

    update = {
      'TableName': self._trails_table,
      'Key': _ddb_item({'trail_id': trail_id}),
      'ConditionExpression': 'attribute_exists(trail_id)',
      'UpdateExpression': update_expr,
      'ExpressionAttributeNames': {'#k': 'end'},
      'ExpressionAttributeValues': expr_values,
    }

    try:
      await asyncio.to_thread(
        self._dynamo.transact_write_items,
        TransactItems=[
          {
            'Put': {
              'TableName': self._steps_table,
              'Item': _ddb_item(step_item),
              'ConditionExpression': 'attribute_not_exists(step_id)',
            }
          },
          {'Update': update},
        ],
      )
    except self._dynamo.exceptions.TransactionCanceledException as e:
      # item 0 is the end-step Put: a retried end POST reuses the same step_id,
      # so a failed condition means it already landed — idempotent success
      # (the end count was not double-incremented).
      codes = _cancellation_codes(e)
      if len(codes) > 0 and codes[0] == 'ConditionalCheckFailed':
        return {'ended_at': ts, 'duplicate': True}
      if _conditional_check_failed(e):
        raise TrailNotFound(trail_id) from e
      raise
    return {'ended_at': ts}

  async def get_trail(self, trail_id: str) -> Optional[dict]:
    response = await asyncio.to_thread(
      self._dynamo.get_item,
      TableName=self._trails_table,
      Key=_ddb_item({'trail_id': trail_id}),
    )
    return _from_ddb_item(response.get('Item'))

  async def query_steps(
    self,
    trail_id: str,
    *,
    after: Optional[str],
    limit: int,
  ) -> dict:
    kwargs: dict = {
      'TableName': self._steps_table,
      'KeyConditionExpression': 'trail_id = :tid',
      'ExpressionAttributeValues': {':tid': _ddb(trail_id)},
      'Limit': limit,
    }
    if after is not None:
      kwargs['ExclusiveStartKey'] = _ddb_item({'trail_id': trail_id, 'step_id': after})

    response = await asyncio.to_thread(self._dynamo.query, **kwargs)
    items = [_from_ddb_item(it) or {} for it in response.get('Items', [])]
    resolved = await asyncio.gather(*(self._resolve_body(item) for item in items))
    next_cursor = None
    last = response.get('LastEvaluatedKey')
    if last is not None:
      next_cursor = _from_ddb(last['step_id'])
    return {'steps': resolved, 'next': next_cursor}

  async def list_trails(
    self,
    *,
    bro: Optional[str],
    parent: Optional[str],
    since: Optional[str],
    until: Optional[str],
    cursor: Optional[str],
    limit: int,
  ) -> dict:
    if bro is not None:
      response = await asyncio.to_thread(
        self._dynamo.query,
        **_range_query(
          table=self._trails_table,
          index='bro-started_at-index',
          pk_name='bro',
          pk_value=bro,
          sk_name='started_at',
          sk_low=since,
          sk_high=until,
          limit=limit,
          cursor=cursor,
        ),
      )
    elif parent is not None:
      response = await asyncio.to_thread(
        self._dynamo.query,
        **_range_query(
          table=self._trails_table,
          index='parent-trail-id-index',
          pk_name='parent_trail_id',
          pk_value=parent,
          sk_name='started_at',
          sk_low=since,
          sk_high=until,
          limit=limit,
          cursor=cursor,
        ),
      )
    else:
      response = await asyncio.to_thread(
        self._dynamo.query,
        **_range_query(
          table=self._trails_table,
          index='all-index',
          pk_name=GSI_PK_ATTR,
          pk_value=GSI_PK_VALUE,
          sk_name='started_at',
          sk_low=since,
          sk_high=until,
          limit=limit,
          cursor=cursor,
        ),
      )

    items = [_from_ddb_item(it) or {} for it in response.get('Items', [])]
    next_cursor = None
    last = response.get('LastEvaluatedKey')
    if last is not None:
      # every path is a GSI query, so the LEK is a triple (base PK trail_id +
      # index PK bro/parent_trail_id/gsi_pk + index SK started_at). all attrs are
      # strings, so the dump is JSON-safe and decodes uniformly via
      # _ddb_item(json.loads(cursor)) in _range_query.
      next_cursor = json.dumps(_from_ddb_item(last))
    return {'trails': items, 'next': next_cursor}

  async def _resolve_body(self, item: dict) -> dict:
    # body_s3 is a server-internal helper attr marking a spilled body; pop it so
    # it never leaks into the step row returned to clients.
    key = item.pop('body_s3', None)
    if key is None:
      return item
    head = await asyncio.to_thread(self._s3.head_object, Bucket=self._bucket, Key=key)
    size = int(head.get('ContentLength', 0))
    if size <= INLINE_RESPONSE_THRESHOLD_BYTES:
      obj = await asyncio.to_thread(self._s3.get_object, Bucket=self._bucket, Key=key)
      raw = obj['Body'].read()
      try:
        item['body'] = json.loads(raw)
      except json.JSONDecodeError:
        item['body'] = raw.decode('utf-8')
    else:
      url = await asyncio.to_thread(
        self._s3.generate_presigned_url,
        ClientMethod='get_object',
        Params={'Bucket': self._bucket, 'Key': key},
        ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
      )
      item['body'] = {'s3': key, 'url': url, 'size': size}
    return item


def _range_query(
  *,
  table: str,
  index: str,
  pk_name: str,
  pk_value: str,
  sk_name: str,
  sk_low: Optional[str],
  sk_high: Optional[str],
  limit: int,
  cursor: Optional[str],
) -> dict:
  values: dict = {':pk': _ddb(pk_value)}
  if sk_low is not None and sk_high is not None:
    cond = f'{pk_name} = :pk AND {sk_name} BETWEEN :lo AND :hi'
    values[':lo'] = _ddb(sk_low)
    values[':hi'] = _ddb(sk_high)
  elif sk_low is not None:
    cond = f'{pk_name} = :pk AND {sk_name} >= :lo'
    values[':lo'] = _ddb(sk_low)
  elif sk_high is not None:
    cond = f'{pk_name} = :pk AND {sk_name} <= :hi'
    values[':hi'] = _ddb(sk_high)
  else:
    cond = f'{pk_name} = :pk'
  kwargs: dict = {
    'TableName': table,
    'IndexName': index,
    'KeyConditionExpression': cond,
    'ExpressionAttributeValues': values,
    'ScanIndexForward': False,
    'Limit': limit,
  }
  if cursor is not None:
    # cursor on a GSI must include the base table's PK plus the index's
    # PK+SK; we encode/decode that triple as a single JSON string.
    kwargs['ExclusiveStartKey'] = _ddb_item(json.loads(cursor))
  return kwargs


def _conditional_check_failed(exc) -> bool:
  reasons = getattr(exc, 'response', {}).get('CancellationReasons', [])
  return any(r.get('Code') == 'ConditionalCheckFailed' for r in reasons)


def _cancellation_codes(exc) -> list[str]:
  # per-item failure codes, positionally aligned with the TransactItems list —
  # lets a caller tell which leg of the transaction tripped its condition.
  reasons = getattr(exc, 'response', {}).get('CancellationReasons', [])
  return [r.get('Code', 'None') for r in reasons]
