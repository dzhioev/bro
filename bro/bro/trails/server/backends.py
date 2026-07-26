"""Harness-specific trail body storage and message projection."""

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from trails.server import storage_types

CLAUDE_ARTIFACT_CONTENT_TYPE = 'application/x-ndjson'


def claude_artifact_key(trail_id: str) -> str:
  return f'trails/claude/{trail_id}/records.jsonl'


def claude_context_key(trail_id: str) -> str:
  return f'trails/claude/{trail_id}/launch-context.json'


def _parse_line(raw: str) -> Optional[dict]:
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return parsed if isinstance(parsed, dict) else None


def _page_start(after: Optional[str]) -> int:
  return int(after) + 1 if after is not None else 0


def _assistant_message_id(record: Optional[dict]) -> Optional[str]:
  if record is None or record.get('type') != 'assistant':
    return None
  message = record.get('message')
  message_id = message.get('id') if isinstance(message, dict) else None
  return message_id if isinstance(message_id, str) else None


def _message_id_before(lines: list[str], start: int) -> Optional[str]:
  """the message id still in flight at line `start`: claude interleaves an
  assistant message's records with the tool-result records it triggers, so the
  nearest assistant record carries it, not necessarily the previous line."""
  for index in range(min(start, len(lines)) - 1, -1, -1):
    message_id = _assistant_message_id(_parse_line(lines[index]))
    if message_id is not None:
      return message_id
  return None


def _source(record: dict, index: int = 0) -> dict:
  return {'step_id': record['step_id'], 'index': index}


def _event(record: dict, event_type: str, index: int = 0, **fields: Any) -> dict:
  return {'type': event_type, 'ts': record.get('ts'), 'source': _source(record, index), **fields}


def _openai_usage(response: dict) -> tuple[str, dict]:
  model = response.get('model', 'unknown')
  usage = response.get('usage')
  return str(model), usage if isinstance(usage, dict) else {}


def normalise_usage(harness: str, usage_by_model: dict) -> dict[str, dict[str, int]]:
  projected: dict[str, dict[str, int]] = {}
  for model, raw in usage_by_model.items():
    if not isinstance(raw, dict):
      raise ValueError(f'usage for model {model!r} must be an object')
    if harness == 'bro':
      details = raw.get('input_tokens_details', {})
      cached = int(details.get('cached_tokens', 0)) if isinstance(details, dict) else 0
      input_tokens = int(raw.get('input_tokens', 0))
      projected[model] = {
        'input': input_tokens - cached,
        'cache_write': 0,
        'cache_read': cached,
        'output': int(raw.get('output_tokens', 0)),
      }
    elif harness == 'claude':
      projected[model] = {
        'input': int(raw.get('input_tokens', 0)),
        'cache_write': int(raw.get('cache_creation_input_tokens', 0)),
        'cache_read': int(raw.get('cache_read_input_tokens', 0)),
        'output': int(raw.get('output_tokens', 0)),
      }
    else:
      raise ValueError(f'unsupported harness: {harness}')
  return projected


def add_numeric_maps(left: dict, right: dict) -> dict:
  result = dict(left)
  for key, value in right.items():
    current = result.get(key)
    if isinstance(value, dict):
      result[key] = add_numeric_maps(current if isinstance(current, dict) else {}, value)
    elif isinstance(value, int) and not isinstance(value, bool):
      result[key] = int(current) + value if isinstance(current, int) else value
  return result


@dataclass(frozen=True)
class OpenBody:
  native: dict
  transaction_items: list[dict]


class BodyBackend(ABC):
  harness: str

  @abstractmethod
  async def open(self, trail_id: str, body: dict, native: dict, started_at: str) -> OpenBody:
    """Prepare the harness body and return its native map plus atomic writes."""
    ...

  @abstractmethod
  async def replace_or_append_body(
    self, trail_id: str, body: Any, *, append: bool, metadata: dict
  ) -> dict:
    """Write body content and return server-derived mutable native fields."""
    ...

  @abstractmethod
  async def iterate_native_records(
    self, trail_id: str, *, after: Optional[str], limit: int
  ) -> dict: ...

  @abstractmethod
  def project_messages(self, records: list[dict]) -> list[dict]: ...

  async def project_message_page(self, trail_id: str, *, after: Optional[str], limit: int) -> dict:
    """one page of generalized events plus the native cursor. Owned by the
    backend because a projection whose state spans records must carry that
    state across the page boundary."""
    page = await self.iterate_native_records(trail_id, after=after, limit=limit)
    return {'messages': self.project_messages(page['steps']), 'next': page['next']}

  def derive_aggregates(self, native: dict) -> dict:
    raw_usage = native.get('usage', {})
    usage = normalise_usage(self.harness, raw_usage if isinstance(raw_usage, dict) else {})
    return {'usage': usage, 'models': sorted(usage)}


class BroBackend(BodyBackend):
  harness = 'bro'

  def __init__(self, *, dynamo, s3, trails_table: str, steps_table: str, bucket: str):
    self._dynamo = dynamo
    self._s3 = s3
    self._trails_table = trails_table
    self._steps_table = steps_table
    self._bucket = bucket

  async def open(self, trail_id: str, body: dict, native: dict, started_at: str) -> OpenBody:
    system_prompt = body.get('system_prompt')
    if not isinstance(system_prompt, str):
      raise ValueError('bro body.system_prompt must be a string')
    step_id = storage_types.new_id()
    item = {
      'trail_id': trail_id,
      'step_id': step_id,
      'ts': started_at,
      'kind': 'system_prompt',
      'body': system_prompt,
      'turn_index': 0,
    }
    return OpenBody(
      native={
        **native,
        'step_counts_by_kind': dict.fromkeys(storage_types.BRO_STEP_KINDS, 0)
        | {'system_prompt': 1},
        'usage': {},
      },
      transaction_items=[
        {'Put': {'TableName': self._steps_table, 'Item': storage_types.ddb_item(item)}}
      ],
    )

  async def replace_or_append_body(
    self, trail_id: str, body: Any, *, append: bool, metadata: dict
  ) -> dict:
    if not append:
      raise ValueError('bro bodies are append-only')
    kind = metadata['kind']
    step_id = metadata.get('step_id')
    step_id = step_id if step_id is not None else storage_types.new_id()
    timestamp = metadata.get('ts')
    timestamp = timestamp if timestamp is not None else storage_types.now_iso()
    size_bytes = storage_types.body_size_bytes(body)
    if size_bytes > storage_types.MAX_BODY_BYTES:
      raise storage_types.BodyTooLarge(
        f'body size {size_bytes} exceeds {storage_types.MAX_BODY_BYTES}'
      )

    body_s3: Optional[str] = None
    if size_bytes >= storage_types.SPILLOVER_THRESHOLD_BYTES:
      body_s3 = storage_types.bro_spillover_key(trail_id, step_id)
      payload = body if isinstance(body, (bytes, str)) else json.dumps(body, ensure_ascii=False)
      if isinstance(payload, str):
        payload = payload.encode('utf-8')
      await asyncio.to_thread(
        self._s3.put_object,
        Bucket=self._bucket,
        Key=body_s3,
        Body=payload,
        ContentType='application/json',
      )

    extras = {key: value for key, value in metadata.items() if key not in {'kind', 'step_id', 'ts'}}
    step = {
      'trail_id': trail_id,
      'step_id': step_id,
      'ts': timestamp,
      'kind': kind,
      **extras,
    }
    if body_s3 is None:
      step['body'] = body
    else:
      step['body_s3'] = body_s3

    usage_delta = self._usage_delta(kind, body)
    while True:
      header_response = await asyncio.to_thread(
        self._dynamo.get_item,
        TableName=self._trails_table,
        Key=storage_types.ddb_item({'id': trail_id}),
        ConsistentRead=True,
      )
      header = storage_types.from_ddb_item(header_response.get('Item'))
      if header is None:
        raise storage_types.TrailNotFound(trail_id)
      names = {'#kind': kind}
      values = {':one': storage_types.ddb(1), ':alive': storage_types.ddb(timestamp)}
      update_parts = [
        'native.step_counts_by_kind.#kind = native.step_counts_by_kind.#kind + :one',
        'last_alive_at = :alive',
      ]
      condition = 'attribute_exists(id)'
      if kind == 'user_input':
        update_parts.append('turn_count = turn_count + :one')
      if usage_delta is not None:
        model, delta = usage_delta
        usage = header.get('native', {}).get('usage', {})
        old_usage = usage.get(model) if isinstance(usage, dict) else None
        accumulated = add_numeric_maps(old_usage if isinstance(old_usage, dict) else {}, delta)
        names['#model'] = model
        names['#usage'] = 'usage'
        values[':usage'] = storage_types.ddb(accumulated)
        values[':old_usage'] = storage_types.ddb(old_usage)
        update_parts.append('native.#usage.#model = :usage')
        condition += (
          ' AND (attribute_not_exists(native.#usage.#model) OR native.#usage.#model = :old_usage)'
        )
      update = {
        'TableName': self._trails_table,
        'Key': storage_types.ddb_item({'id': trail_id}),
        'ConditionExpression': condition,
        'UpdateExpression': 'SET ' + ', '.join(update_parts),
        'ExpressionAttributeNames': names,
        'ExpressionAttributeValues': values,
      }
      try:
        await asyncio.to_thread(
          self._dynamo.transact_write_items,
          TransactItems=[
            {
              'Put': {
                'TableName': self._steps_table,
                'Item': storage_types.ddb_item(step),
                'ConditionExpression': 'attribute_not_exists(step_id)',
              }
            },
            {'Update': update},
          ],
        )
      except self._dynamo.exceptions.TransactionCanceledException as exception:
        codes = storage_types.cancellation_codes(exception)
        if len(codes) > 0 and codes[0] == 'ConditionalCheckFailed':
          return {'step_id': step_id, 'ts': timestamp, 'duplicate': True}
        if len(codes) > 1 and codes[1] == 'ConditionalCheckFailed' and usage_delta is not None:
          continue
        if storage_types.conditional_check_failed(exception):
          raise storage_types.TrailNotFound(trail_id) from exception
        raise
      return {'step_id': step_id, 'ts': timestamp}

  def _usage_delta(self, kind: str, body: Any) -> Optional[tuple[str, dict]]:
    if kind != 'llm_call' or not isinstance(body, dict):
      return None
    response = body.get('response')
    if not isinstance(response, dict):
      return None
    model, usage = _openai_usage(response)
    return model, usage

  async def iterate_native_records(
    self, trail_id: str, *, after: Optional[str], limit: int
  ) -> dict:
    kwargs: dict = {
      'TableName': self._steps_table,
      'KeyConditionExpression': 'trail_id = :trail_id',
      'ExpressionAttributeValues': {':trail_id': storage_types.ddb(trail_id)},
      'Limit': limit,
    }
    if after is not None:
      kwargs['ExclusiveStartKey'] = storage_types.ddb_item({'trail_id': trail_id, 'step_id': after})
    response = await asyncio.to_thread(self._dynamo.query, **kwargs)
    records = [storage_types.from_ddb_item(item) for item in response.get('Items', [])]
    resolved = await asyncio.gather(
      *(self._resolve_body(record) for record in records if record is not None)
    )
    last = response.get('LastEvaluatedKey')
    next_cursor = storage_types.from_ddb(last['step_id']) if last is not None else None
    return {'steps': resolved, 'next': next_cursor}

  async def _resolve_body(self, item: dict) -> dict:
    key = item.pop('body_s3', None)
    if key is None:
      return item
    head = await asyncio.to_thread(self._s3.head_object, Bucket=self._bucket, Key=key)
    size = int(head.get('ContentLength', 0))
    if size <= storage_types.INLINE_RESPONSE_THRESHOLD_BYTES:
      stored = await asyncio.to_thread(self._s3.get_object, Bucket=self._bucket, Key=key)
      raw = stored['Body'].read()
      try:
        item['body'] = json.loads(raw)
      except json.JSONDecodeError:
        item['body'] = raw.decode('utf-8')
    else:
      url = await asyncio.to_thread(
        self._s3.generate_presigned_url,
        ClientMethod='get_object',
        Params={'Bucket': self._bucket, 'Key': key},
        ExpiresIn=storage_types.PRESIGNED_URL_TTL_SECONDS,
      )
      item['body'] = {'s3': key, 'url': url, 'size': size}
    return item

  def project_messages(self, records: list[dict]) -> list[dict]:
    messages: list[dict] = []
    for record in records:
      kind = record['kind']
      body = record.get('body')
      if kind == 'llm_call':
        response = body.get('response', {}) if isinstance(body, dict) else {}
        model, raw_usage = _openai_usage(response if isinstance(response, dict) else {})
        messages.append(
          _event(
            record,
            'llm_call',
            model=model,
            usage=normalise_usage('bro', {model: raw_usage}).get(model, {}),
          )
        )
      elif kind == 'end':
        reason = body.get('reason') if isinstance(body, dict) else None
        messages.append(_event(record, 'end', reason='ok' if reason == 'terminal' else reason))
      elif kind in storage_types.MESSAGE_TYPES:
        fields: dict[str, Any] = {'content': body}
        for key in ('tool_name', 'arguments', 'call_id', 'is_error', 'terminal'):
          if key in record:
            fields[key] = record[key]
        messages.append(_event(record, kind, **fields))
      else:
        messages.append(_event(record, 'harness_event', raw=record))
    return messages


class ClaudeBackend(BodyBackend):
  harness = 'claude'

  def __init__(self, *, s3, bucket: str):
    self._s3 = s3
    self._bucket = bucket

  async def open(self, trail_id: str, body: dict, native: dict, started_at: str) -> OpenBody:
    del started_at
    artifact = body.get('artifact', '')
    if not isinstance(artifact, str):
      raise ValueError('claude body.artifact must be a string')
    artifact_key = claude_artifact_key(trail_id)
    await asyncio.to_thread(
      self._s3.put_object,
      Bucket=self._bucket,
      Key=artifact_key,
      Body=artifact.encode('utf-8'),
      ContentType=CLAUDE_ARTIFACT_CONTENT_TYPE,
    )
    opened_native = {
      **native,
      's3_key': artifact_key,
      'line_count': len(artifact.splitlines()),
      'size_bytes': len(artifact.encode('utf-8')),
      'usage': native.get('usage', {}),
    }
    if 'launch_context' in body:
      context_key = claude_context_key(trail_id)
      payload = json.dumps(body['launch_context'], ensure_ascii=False).encode('utf-8')
      await asyncio.to_thread(
        self._s3.put_object,
        Bucket=self._bucket,
        Key=context_key,
        Body=payload,
        ContentType='application/json',
      )
      opened_native['context_s3'] = context_key
    return OpenBody(native=opened_native, transaction_items=[])

  async def replace_or_append_body(
    self, trail_id: str, body: Any, *, append: bool, metadata: dict
  ) -> dict:
    if append:
      raise ValueError('claude artifacts are replaced as complete snapshots')
    if not isinstance(body, str):
      raise ValueError('claude artifact must be a string')
    payload = body.encode('utf-8')
    await asyncio.to_thread(
      self._s3.put_object,
      Bucket=self._bucket,
      Key=claude_artifact_key(trail_id),
      Body=payload,
      ContentType=CLAUDE_ARTIFACT_CONTENT_TYPE,
    )
    return {'line_count': len(body.splitlines()), 'size_bytes': len(payload), **metadata}

  async def read_context(self, context_key: str) -> Any:
    """the stored launch-context document at `context_key` (`native.context_s3`)."""
    response = await asyncio.to_thread(self._s3.get_object, Bucket=self._bucket, Key=context_key)
    return json.loads(response['Body'].read().decode('utf-8'))

  async def _artifact_lines(self, trail_id: str) -> list[str]:
    response = await asyncio.to_thread(
      self._s3.get_object, Bucket=self._bucket, Key=claude_artifact_key(trail_id)
    )
    return response['Body'].read().decode('utf-8').splitlines()

  async def iterate_native_records(
    self, trail_id: str, *, after: Optional[str], limit: int
  ) -> dict:
    return self._page(trail_id, await self._artifact_lines(trail_id), after=after, limit=limit)

  async def project_message_page(self, trail_id: str, *, after: Optional[str], limit: int) -> dict:
    lines = await self._artifact_lines(trail_id)
    page = self._page(trail_id, lines, after=after, limit=limit)
    return {
      'messages': self._project(
        page['steps'], billed=_message_id_before(lines, _page_start(after))
      ),
      'next': page['next'],
    }

  @staticmethod
  def _page(trail_id: str, lines: list[str], *, after: Optional[str], limit: int) -> dict:
    start = _page_start(after)
    selected = lines[start : start + limit]
    records = []
    for offset, raw in enumerate(selected, start=start):
      parsed = _parse_line(raw)
      records.append(
        {
          'trail_id': trail_id,
          'step_id': str(offset),
          'ts': parsed.get('timestamp') if isinstance(parsed, dict) else None,
          'raw': raw,
          'record': parsed,
        }
      )
    next_cursor = str(start + len(selected) - 1) if start + len(selected) < len(lines) else None
    return {'steps': records, 'next': next_cursor}

  def project_messages(self, records: list[dict]) -> list[dict]:
    return self._project(records, billed=None)

  def _project(self, records: list[dict], *, billed: Optional[str]) -> list[dict]:
    """`billed` is the message id of the record preceding `records` — claude
    splits one API message across adjacent records (one per content block),
    each repeating the message's id and usage, and only a message id's first
    record emits the llm_call, so neither type filters nor page boundaries can
    multiply totals."""
    messages: list[dict] = []
    last_message_id: Optional[str] = billed
    for native in records:
      record = native.get('record')
      if not isinstance(record, dict):
        messages.append(_event(native, 'harness_event', raw=native.get('raw')))
        continue
      record_type = record.get('type')
      message = record.get('message')
      if record_type == 'assistant' and isinstance(message, dict):
        message_id = message.get('id')
        first_of_message = not isinstance(message_id, str) or message_id != last_message_id
        if isinstance(message_id, str):
          last_message_id = message_id
        messages.extend(
          self._assistant_messages(native, record, message, first_of_message=first_of_message)
        )
      elif record_type == 'user' and isinstance(message, dict):
        messages.extend(self._user_messages(native, record, message))
      else:
        messages.append(_event(native, 'harness_event', raw=record))
    return messages

  def _assistant_messages(
    self, native: dict, record: dict, message: dict, *, first_of_message: bool
  ) -> list[dict]:
    if record.get('isApiErrorMessage') is True:
      return [_event(native, 'error', content=message.get('content'))]
    model = str(message.get('model', 'unknown'))
    raw_usage = message.get('usage')
    if not isinstance(raw_usage, dict) or model == '<synthetic>':
      return [_event(native, 'harness_event', raw=record)]
    events: list[dict] = []
    if first_of_message:
      events.append(
        _event(
          native,
          'llm_call',
          model=model,
          usage=normalise_usage('claude', {model: raw_usage}).get(model, {}),
        )
      )
    content = message.get('content')
    if not isinstance(content, list):
      return [*events, _event(native, 'harness_event', 1, raw=record)]
    for index, block in enumerate(content, start=1):
      if not isinstance(block, dict):
        events.append(_event(native, 'harness_event', index, raw=block))
      elif block.get('type') == 'thinking':
        events.append(_event(native, 'reasoning', index, content=block.get('thinking')))
      elif block.get('type') == 'text':
        events.append(_event(native, 'assistant', index, content=block.get('text')))
      elif block.get('type') == 'tool_use':
        events.append(
          _event(
            native,
            'tool_call',
            index,
            tool_name=block.get('name'),
            call_id=block.get('id'),
            arguments=block.get('input'),
          )
        )
      else:
        events.append(_event(native, 'harness_event', index, raw=block))
    return events

  def _user_messages(self, native: dict, record: dict, message: dict) -> list[dict]:
    content = message.get('content')
    if isinstance(content, list) and all(
      isinstance(block, dict) and block.get('type') == 'tool_result' for block in content
    ):
      return [
        _event(
          native,
          'tool_result',
          index,
          call_id=block.get('tool_use_id'),
          content=block.get('content'),
          is_error=block.get('is_error', False),
        )
        for index, block in enumerate(content)
      ]
    return [
      _event(
        native,
        'user_input',
        content=content,
        isMeta=record.get('isMeta', False),
        isSidechain=record.get('isSidechain', False),
      )
    ]
