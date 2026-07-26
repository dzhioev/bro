import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from trails.server import storage
from trails.server.backends import (
  BroBackend,
  ClaudeBackend,
  add_numeric_maps,
  claude_artifact_key,
  claude_context_key,
  normalise_usage,
)

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _serialize(item: dict) -> dict:
  return {key: _serializer.serialize(value) for key, value in item.items()}


def _deserialize(item: dict) -> dict:
  return {key: _deserializer.deserialize(value) for key, value in item.items()}


class FakeS3:
  def __init__(self):
    self.objects: dict[str, bytes] = {}

  def put_object(self, *, Key, Body, **_):
    self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

  def get_object(self, *, Key, **_):
    return {'Body': io.BytesIO(self.objects[Key])}

  def head_object(self, *, Key, **_):
    return {'ContentLength': len(self.objects[Key])}

  def generate_presigned_url(self, **_):
    return 'https://example.test/spill'


_RESERVED_WORDS = frozenset(
  (Path(__file__).parent / 'dynamodb_reserved_words.txt').read_text().split()
)
# uppercase-only, as the expressions in this repo spell them; a lowercase attribute
# that collides with a keyword still gets flagged through the reserved-word check
_EXPRESSION_KEYWORDS = frozenset(
  {'SET', 'REMOVE', 'ADD', 'DELETE', 'AND', 'OR', 'NOT', 'BETWEEN', 'IN'}
)
_EXPRESSION_FUNCTIONS = frozenset(
  {'attribute_exists', 'attribute_not_exists', 'attribute_type', 'begins_with', 'contains', 'size'}
)
_EXPRESSION_TOKEN = re.compile(r'[#:]?[A-Za-z_][A-Za-z0-9_]*')


def _validate_expressions(operation: dict) -> None:
  """reject expressions real DynamoDB would: bare reserved words, undefined or
  unused ExpressionAttributeNames / ExpressionAttributeValues entries."""
  names = operation.get('ExpressionAttributeNames', {})
  values = operation.get('ExpressionAttributeValues', {})
  used_names: set[str] = set()
  used_values: set[str] = set()
  for key, expression in operation.items():
    if not key.endswith('Expression') or expression is None:
      continue
    for token in _EXPRESSION_TOKEN.findall(expression):
      if token.startswith('#'):
        if token not in names:
          raise AssertionError(f'{key} references undefined name {token}: {expression}')
        used_names.add(token)
      elif token.startswith(':'):
        if token not in values:
          raise AssertionError(f'{key} references undefined value {token}: {expression}')
        used_values.add(token)
      elif token in _EXPRESSION_KEYWORDS or token in _EXPRESSION_FUNCTIONS:
        continue
      elif token.upper() in _RESERVED_WORDS:
        raise AssertionError(f'{key} uses reserved word {token!r}: {expression}')
  if used_names != set(names):
    raise AssertionError(f'unused ExpressionAttributeNames: {sorted(set(names) - used_names)}')
  if used_values != set(values):
    raise AssertionError(f'unused ExpressionAttributeValues: {sorted(set(values) - used_values)}')


class FakeDynamo:
  class exceptions:
    class ConditionalCheckFailedException(Exception):
      pass

    class TransactionCanceledException(Exception):
      pass

  def __init__(self):
    self.headers: dict[str, dict] = {}
    self.steps: dict[tuple[str, str], dict] = {}

  def put_item(self, *, TableName, Item, ConditionExpression=None):
    _validate_expressions({'ConditionExpression': ConditionExpression})
    item = _deserialize(Item)
    if TableName == 'headers':
      if ConditionExpression is not None and item['id'] in self.headers:
        raise self.exceptions.ConditionalCheckFailedException()
      self.headers[item['id']] = item
    else:
      self.steps[(item['trail_id'], item['step_id'])] = item

  def get_item(self, *, TableName, Key, **_):
    key = _deserialize(Key)
    item = self.headers.get(key['id']) if TableName == 'headers' else None
    return {'Item': _serialize(item)} if item is not None else {}

  def transact_write_items(self, *, TransactItems):
    for transact_item in TransactItems:
      for operation in transact_item.values():
        _validate_expressions(operation)
    first_put = TransactItems[0].get('Put')
    if first_put is not None and first_put['TableName'] == 'headers':
      header = _deserialize(first_put['Item'])
      if header['id'] in self.headers:
        self._cancel(['ConditionalCheckFailed', 'None'])
      self.headers[header['id']] = header
      for operation in TransactItems[1:]:
        step = _deserialize(operation['Put']['Item'])
        self.steps[(step['trail_id'], step['step_id'])] = step
      return
    put = TransactItems[0]['Put']
    update = TransactItems[1]['Update']
    step = _deserialize(put['Item'])
    step_key = (step['trail_id'], step['step_id'])
    header_id = _deserialize(update['Key'])['id']
    if step_key in self.steps:
      self._cancel(['ConditionalCheckFailed', 'None'])
    header = self.headers.get(header_id)
    if header is None:
      self._cancel(['None', 'ConditionalCheckFailed'])
    assert header is not None
    names = update['ExpressionAttributeNames']
    values = {
      key: _deserializer.deserialize(value)
      for key, value in update['ExpressionAttributeValues'].items()
    }
    kind = names['#kind']
    header['native']['step_counts_by_kind'][kind] += values[':one']
    header['last_alive_at'] = values[':alive']
    if 'turn_count = turn_count + :one' in update['UpdateExpression']:
      header['turn_count'] += values[':one']
    model = names.get('#model')
    if model is not None:
      header['native']['usage'][model] = values[':usage']
    self.steps[step_key] = step

  def _cancel(self, codes: list[str]):
    exception = self.exceptions.TransactionCanceledException('cancelled')
    exception.response = {'CancellationReasons': [{'Code': code} for code in codes]}  # type: ignore[attr-defined]
    raise exception

  def update_item(self, *, TableName, Key, UpdateExpression, ExpressionAttributeValues, **kwargs):
    _validate_expressions(
      {
        'UpdateExpression': UpdateExpression,
        'ExpressionAttributeValues': ExpressionAttributeValues,
        **kwargs,
      }
    )
    del TableName
    trail_id = _deserialize(Key)['id']
    header = self.headers.get(trail_id)
    if header is None:
      raise self.exceptions.ConditionalCheckFailedException()
    values = {
      key: _deserializer.deserialize(value) for key, value in ExpressionAttributeValues.items()
    }
    names = kwargs.get('ExpressionAttributeNames', {})
    if UpdateExpression == 'SET last_alive_at = :timestamp':
      header['last_alive_at'] = values[':timestamp']
    elif UpdateExpression == 'SET #end = :end, last_alive_at = :timestamp':
      header['end'] = values[':end']
      header['last_alive_at'] = values[':timestamp']
    elif UpdateExpression == 'SET #end = :end':
      if header.get('end') is not None:
        raise self.exceptions.ConditionalCheckFailedException()
      header['end'] = values[':end']
    else:
      assignments = UpdateExpression.removeprefix('SET ').split(', ')
      for assignment in assignments:
        target, value_name = assignment.split(' = ')
        field = names[target.removeprefix('native.').strip()]
        if target.startswith('native.'):
          header['native'][field] = values[value_name]
        else:
          header[field] = values[value_name]
    return {}

  def query(self, **kwargs):
    _validate_expressions(kwargs)
    values = {
      key: _deserializer.deserialize(value)
      for key, value in kwargs['ExpressionAttributeValues'].items()
    }
    if 'IndexName' not in kwargs:
      trail_id = values[':trail_id']
      ordered = sorted(
        [item for (item_trail_id, _), item in self.steps.items() if item_trail_id == trail_id],
        key=lambda item: item['step_id'],
      )
      start_key = kwargs.get('ExclusiveStartKey')
      if start_key is not None:
        after = _deserialize(start_key)['step_id']
        ordered = [item for item in ordered if item['step_id'] > after]
      limit = kwargs['Limit']
      page = ordered[:limit]
      response: dict[str, Any] = {'Items': [_serialize(item) for item in page]}
      if len(ordered) > limit:
        response['LastEvaluatedKey'] = _serialize(
          {'trail_id': trail_id, 'step_id': page[-1]['step_id']}
        )
      return response
    index = kwargs['IndexName']
    partition_name = {
      'all-index': 'gsi_pk',
      'bro-started_at-index': 'bro',
      'harness-started_at-index': 'harness',
      'forked-from-id-index': 'forked_from_id',
    }[index]
    items = [
      item for item in self.headers.values() if item.get(partition_name) == values[':partition']
    ]
    if ':since' in values:
      items = [item for item in items if item['started_at'] >= values[':since']]
    if ':until' in values:
      items = [item for item in items if item['started_at'] <= values[':until']]
    items.sort(key=lambda item: item['started_at'], reverse=True)
    return {'Items': [_serialize(item) for item in items[: kwargs['Limit']]]}


@pytest.fixture
def components():
  dynamo = FakeDynamo()
  s3 = FakeS3()
  store = storage.Storage(
    dynamo=dynamo,
    s3=s3,
    trails_table='headers',
    steps_table='steps',
    bucket='bucket',
  )
  return store, dynamo, s3


async def _create_bro(store: storage.Storage, **overrides) -> str:
  payload = {
    'harness': 'bro',
    'version': '2',
    'bro': 'dev',
    'interactive': False,
    'surface': 'ask',
    'hold': 'unattended',
    'native': {'llm': {'type': 'chat_gpt', 'model': 'gpt-5'}},
    'body': {'system_prompt': 'prompt'},
  }
  payload.update(overrides)
  return (await store.create_trail(**payload))['id']


@pytest.mark.asyncio
async def test_create_stores_universal_header_and_opens_bro_body(components):
  store, dynamo, _ = components
  trail_id = await _create_bro(
    store,
    forked_from={'trail_id': 'parent', 'step_id': 'step'},
    summoned_by={'trail_id': 'summoner'},
  )
  header = dynamo.headers[trail_id]
  assert header['id'] == trail_id
  assert header['harness'] == 'bro'
  assert header['version'] == '2'
  assert header['forked_from_id'] == 'parent'
  assert header['summoned_by'] == {'trail_id': 'summoner'}
  assert header['native']['step_counts_by_kind']['system_prompt'] == 1
  [system_prompt] = dynamo.steps.values()
  assert system_prompt['kind'] == 'system_prompt'


@pytest.mark.asyncio
async def test_backend_instances_are_cached(components):
  store, _, _ = components
  first = store._backend('bro')
  second = store._backend('bro')
  assert first is second
  assert isinstance(first, BroBackend)


@pytest.mark.asyncio
async def test_bro_step_updates_counts_turns_and_exact_raw_usage(components):
  store, dynamo, _ = components
  trail_id = await _create_bro(store)
  await store.put_step(trail_id=trail_id, kind='user_input', body='hello', extras={})
  response = {
    'model': 'gpt-5',
    'usage': {
      'input_tokens': 100,
      'input_tokens_details': {'cached_tokens': 40},
      'output_tokens': 20,
      'output_tokens_details': {'reasoning_tokens': 7},
      'total_tokens': 120,
    },
  }
  await store.put_step(
    trail_id=trail_id,
    kind='llm_call',
    body={'request': {}, 'response': response},
    extras={},
    step_id='call-1',
  )
  await store.put_step(
    trail_id=trail_id,
    kind='llm_call',
    body={'request': {}, 'response': response},
    extras={},
    step_id='call-2',
  )
  header = dynamo.headers[trail_id]
  assert header['turn_count'] == 1
  assert header['native']['step_counts_by_kind']['llm_call'] == 2
  assert header['native']['usage']['gpt-5']['input_tokens'] == 200
  projected = await store.get_trail(trail_id)
  assert projected['usage']['gpt-5'] == {
    'input': 120,
    'cache_write': 0,
    'cache_read': 80,
    'output': 40,
  }
  assert projected['models'] == ['gpt-5']


@pytest.mark.asyncio
async def test_retried_step_id_is_idempotent(components):
  store, dynamo, _ = components
  trail_id = await _create_bro(store)
  await store.put_step(trail_id=trail_id, kind='reasoning', body='first', extras={}, step_id='same')
  duplicate = await store.put_step(
    trail_id=trail_id, kind='reasoning', body='first', extras={}, step_id='same'
  )
  assert duplicate['duplicate'] is True
  assert len([key for key in dynamo.steps if key[1] == 'same']) == 1


@pytest.mark.asyncio
async def test_bro_spillover_round_trip(components):
  store, _, s3 = components
  trail_id = await _create_bro(store)
  body = {'text': 'x' * (storage.SPILLOVER_THRESHOLD_BYTES + 1)}
  await store.put_step(trail_id=trail_id, kind='tool_result', body=body, extras={})
  assert len(s3.objects) == 1
  page = await store.query_steps(trail_id, after=None, limit=100)
  assert page['steps'][-1]['body'] == body


@pytest.mark.asyncio
async def test_claude_body_keys_snapshot_and_lossless_lines(components):
  store, dynamo, s3 = components
  result = await store.create_trail(
    harness='claude',
    version='2',
    interactive=True,
    surface='cw',
    body={'artifact': '{"type":"system"}\nnot json\n', 'launch_context': {'cwd': '/x'}},
    native={'segment': 'uuid', 'llm': {'model': 'opus'}},
  )
  trail_id = result['id']
  native = dynamo.headers[trail_id]['native']
  assert native['s3_key'] == claude_artifact_key(trail_id)
  assert native['context_s3'] == claude_context_key(trail_id)
  assert claude_context_key(trail_id) in s3.objects
  page = await store.query_steps(trail_id, after=None, limit=100)
  assert page['steps'][0]['record'] == {'type': 'system'}
  assert page['steps'][1]['record'] is None
  assert page['steps'][1]['raw'] == 'not json'
  await store.replace_artifact(
    trail_id,
    '{"type":"user","message":{"content":"hello"}}\n',
    {'harness_version': '2.1.0'},
  )
  assert dynamo.headers[trail_id]['native']['line_count'] == 1
  assert dynamo.headers[trail_id]['native']['harness_version'] == '2.1.0'


@pytest.mark.asyncio
async def test_update_header_rejects_frozen_fields(components):
  store, _, _ = components
  trail_id = await _create_bro(store)
  with pytest.raises(ValueError, match='immutable'):
    await store.update_header(trail_id, {'surface': 'other'})
  updated = await store.update_header(trail_id, {'subject': 'new subject'})
  assert updated['subject'] == 'new subject'


@pytest.mark.asyncio
async def test_end_uses_final_map_and_historical_projection_maps_terminal(components):
  store, _, _ = components
  trail_id = await _create_bro(store)
  await store.end_trail(trail_id=trail_id, reason='raised', detail='blocked', step_id='end')
  header = await store.get_trail(trail_id)
  assert header['end']['reason'] == 'raised'
  assert header['end']['detail'] == 'blocked'
  backend = store._backend('bro')
  messages = backend.project_messages(
    [
      {
        'trail_id': trail_id,
        'step_id': 'old-end',
        'ts': '2026-01-01T00:00:00Z',
        'kind': 'end',
        'body': {'reason': 'terminal'},
      }
    ]
  )
  assert messages[0]['reason'] == 'ok'


@pytest.mark.asyncio
async def test_list_uses_harness_and_fork_indexes(components):
  store, _, _ = components
  root = await _create_bro(store)
  child = await _create_bro(store, forked_from={'trail_id': root, 'step_id': 'step'})
  harness_page = await store.list_trails(
    harness='bro', bro=None, forked_from=None, since=None, until=None, cursor=None, limit=10
  )
  assert {item['id'] for item in harness_page['trails']} == {root, child}
  fork_page = await store.list_trails(
    harness=None,
    bro=None,
    forked_from=root,
    since=None,
    until=None,
    cursor=None,
    limit=10,
  )
  assert [item['id'] for item in fork_page['trails']] == [child]


def test_usage_helpers_preserve_raw_shape_and_project_harnesses():
  assert add_numeric_maps({'a': 1, 'nested': {'x': 2}}, {'a': 3, 'nested': {'x': 4}}) == {
    'a': 4,
    'nested': {'x': 6},
  }
  assert normalise_usage(
    'claude',
    {
      'opus': {
        'input_tokens': 1,
        'cache_creation_input_tokens': 2,
        'cache_read_input_tokens': 3,
        'output_tokens': 4,
      }
    },
  ) == {'opus': {'input': 1, 'cache_write': 2, 'cache_read': 3, 'output': 4}}


def test_claude_projection_emits_llm_call_once_and_content_events():
  backend = ClaudeBackend(s3=None, bucket='bucket')
  messages = backend.project_messages(
    [
      {
        'step_id': '0',
        'ts': '2026-01-01T00:00:00Z',
        'raw': '',
        'record': {
          'type': 'assistant',
          'message': {
            'model': 'opus',
            'usage': {'input_tokens': 1, 'output_tokens': 2},
            'content': [
              {'type': 'thinking', 'thinking': 'hmm'},
              {'type': 'text', 'text': 'answer'},
              {'type': 'tool_use', 'id': 'tool-1', 'name': 'read', 'input': {'path': 'x'}},
            ],
          },
        },
      }
    ]
  )
  assert [message['type'] for message in messages] == [
    'llm_call',
    'reasoning',
    'assistant',
    'tool_call',
  ]
  assert messages[0]['usage']['output'] == 2
  assert messages[2]['source'] == {'step_id': '0', 'index': 2}


def _assistant_record(step_id: str, message_id: str, block: dict) -> dict:
  return {
    'step_id': step_id,
    'ts': '2026-01-01T00:00:00Z',
    'raw': '',
    'record': {
      'type': 'assistant',
      'message': {
        'id': message_id,
        'model': 'opus',
        'usage': {'input_tokens': 1, 'output_tokens': 2},
        'content': [block],
      },
    },
  }


def test_claude_projection_dedups_split_message_records():
  # claude writes one record per content block of the same API message, each
  # repeating the message id and usage — only the first record of a message id
  # may bill, or summing llm_call usage would multiply token totals
  backend = ClaudeBackend(s3=None, bucket='bucket')
  messages = backend.project_messages(
    [
      _assistant_record('0', 'msg-1', {'type': 'thinking', 'thinking': 'hmm'}),
      _assistant_record('1', 'msg-1', {'type': 'text', 'text': 'answer'}),
      _assistant_record('2', 'msg-2', {'type': 'text', 'text': 'more'}),
    ]
  )
  assert [message['type'] for message in messages] == [
    'llm_call',
    'reasoning',
    'assistant',
    'llm_call',
    'assistant',
  ]
  # block events keep their in-record index even when the llm_call is deduped
  assert messages[2]['source'] == {'step_id': '1', 'index': 1}


@pytest.mark.asyncio
async def test_claude_message_pages_bill_a_split_message_once():
  s3 = FakeS3()
  backend = ClaudeBackend(s3=s3, bucket='bucket')
  records = [
    _assistant_record('0', 'msg-1', {'type': 'thinking', 'thinking': 'hmm'})['record'],
    _assistant_record('1', 'msg-1', {'type': 'text', 'text': 'answer'})['record'],
    _assistant_record('2', 'msg-2', {'type': 'text', 'text': 'more'})['record'],
  ]
  s3.put_object(
    Key=claude_artifact_key('trail'),
    Body=''.join(json.dumps(record) + '\n' for record in records),
  )
  billed: list[dict] = []
  after: Any = None
  while True:
    page = await backend.project_message_page('trail', after=after, limit=1)
    billed.extend(message for message in page['messages'] if message['type'] == 'llm_call')
    after = page['next']
    if after is None:
      break
  assert [message['usage']['output'] for message in billed] == [2, 2]


@pytest.mark.asyncio
async def test_launch_context_round_trip(components):
  store, _, _ = components
  claude_native = {'segment': 'uuid', 'llm': {}, 'cw_command': 'cw ss', 'harness_version': '2.1.0'}
  with_context = (
    await store.create_trail(
      harness='claude',
      version='2',
      interactive=True,
      surface='cw',
      native=claude_native,
      body={'artifact': '', 'launch_context': [{'title': 'git state'}]},
    )
  )['id']
  without_context = (
    await store.create_trail(
      harness='claude',
      version='2',
      interactive=True,
      surface='cw',
      native=claude_native,
      body={'artifact': ''},
    )
  )['id']
  assert await store.get_launch_context(with_context) == [{'title': 'git state'}]
  assert await store.get_launch_context(without_context) is None
  bro_trail = await _create_bro(store)
  assert await store.get_launch_context(bro_trail) is None


def test_expression_validation_matches_real_dynamodb_rejections():
  with pytest.raises(AssertionError, match="reserved word 'usage'"):
    _validate_expressions(
      {
        'UpdateExpression': 'SET native.usage.#model = :usage',
        'ExpressionAttributeNames': {'#model': 'opus'},
        'ExpressionAttributeValues': {':usage': {'N': '1'}},
      }
    )
  with pytest.raises(AssertionError, match='undefined name #model'):
    _validate_expressions({'UpdateExpression': 'SET #model = :value'})
  with pytest.raises(AssertionError, match='unused ExpressionAttributeValues'):
    _validate_expressions(
      {
        'ConditionExpression': 'attribute_exists(id)',
        'ExpressionAttributeValues': {':stray': {'N': '1'}},
      }
    )
  _validate_expressions(
    {
      'UpdateExpression': 'SET native.#usage.#model = :usage, last_alive_at = :alive',
      'ExpressionAttributeNames': {'#usage': 'usage', '#model': 'opus'},
      'ExpressionAttributeValues': {':usage': {'N': '1'}, ':alive': {'S': 't'}},
    }
  )
