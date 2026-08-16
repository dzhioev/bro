import copy
import io
import json
import threading
from typing import Any, Optional

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from bro.trails import backends
from bro.trails.model import MESSAGE_TYPES, UNREPORTED_END_INFERENCE, BlazeRequest, tools_sha256
from bro.trails.server import dynamo as dynamo_store, dynamo_types
from bro.trails.store import AppendConflict

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _serialize(item: dict) -> dict:
  return {key: _serializer.serialize(value) for key, value in item.items()}


def _deserialize(item: dict) -> dict:
  return {key: _deserializer.deserialize(value) for key, value in item.items()}


class FakeClientError(Exception):
  def __init__(self, response: dict, operation: str):
    super().__init__(f'{operation}: {response}')
    self.response = response


class FakeS3:
  class exceptions:
    ClientError = FakeClientError

  def __init__(self):
    self.objects: dict[str, bytes] = {}
    self.put_counts: dict[str, int] = {}
    self.get_threads: list[int] = []

  def put_object(self, *, Key, Body, IfNoneMatch=None, **_):
    if IfNoneMatch == '*' and Key in self.objects:
      raise FakeClientError(
        {'Error': {'Code': 'PreconditionFailed', 'Message': 'already exists'}},
        'PutObject',
      )
    self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()
    self.put_counts[Key] = self.put_counts.get(Key, 0) + 1

  def get_object(self, *, Key, **_):
    self.get_threads.append(threading.get_ident())
    if Key not in self.objects:
      raise FakeClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'missing'}}, 'GetObject')
    return {'Body': io.BytesIO(self.objects[Key])}


class FakeDynamo:
  class exceptions:
    class ConditionalCheckFailedException(Exception):
      pass

    class TransactionCanceledException(Exception):
      pass

  def __init__(self):
    self.tables: dict[str, dict[Any, dict]] = {'headers': {}, 'universal': {}}
    self.queries: list[dict] = []
    self.query_threads: list[int] = []
    self.scans: list[dict] = []

  @property
  def headers(self) -> dict[str, dict]:
    return self.tables['headers']

  @property
  def universal_steps(self) -> dict[tuple[str, int], dict]:
    return self.tables['universal']

  @staticmethod
  def _key(table: str, item: dict) -> Any:
    if table == 'headers':
      return item['id']
    return item['trail_id'], item['step_id']

  def _read_key(self, table: str, key: dict) -> Any:
    return self._key(table, _deserialize(key))

  @staticmethod
  def _names(operation: dict) -> dict[str, str]:
    return operation.get('ExpressionAttributeNames') or {}

  @staticmethod
  def _values(operation: dict) -> dict[str, Any]:
    return {
      key: _deserializer.deserialize(value)
      for key, value in (operation.get('ExpressionAttributeValues') or {}).items()
    }

  def _field(self, token: str, operation: dict) -> str:
    return self._names(operation).get(token, token)

  def _condition(self, item: Optional[dict], operation: dict) -> bool:
    expression = operation.get('ConditionExpression')
    if expression is None:
      return True
    values = self._values(operation)
    if expression == 'attribute_not_exists(id)':
      return item is None
    if expression in {'attribute_not_exists(step_id)', 'attribute_not_exists(trail_id)'}:
      return item is None
    if expression == 'attribute_exists(id)':
      return item is not None
    if expression == 'attribute_not_exists(#forked_from)':
      return item is not None and self._field('#forked_from', operation) not in item
    if expression == 'attribute_type(#end, :null_type)':
      return item is not None and item.get(self._field('#end', operation)) is None
    if expression == '#end = :old':
      return item is not None and item.get(self._field('#end', operation)) == values[':old']
    if expression == '#native.#llm = :expected':
      native = item.get(self._field('#native', operation)) if item is not None else None
      return isinstance(native, dict) and native.get('llm') == values[':expected']
    if expression == '#native = :old_native':
      return (
        item is not None and item.get(self._field('#native', operation)) == values[':old_native']
      )
    if expression == '#body_storage = :storage':
      return (
        item is not None and item.get(self._field('#body_storage', operation)) == values[':storage']
      )
    if expression == '#body_storage = :storage AND #extent = :expected_extent':
      return (
        item is not None
        and item.get(self._field('#body_storage', operation)) == values[':storage']
        and item.get(self._field('#extent', operation)) == values[':expected_extent']
      )
    if expression == '#pointer = :before':
      return item is not None and item.get(self._field('#pointer', operation)) == values[':before']
    if expression == (
      '#harness = :bro AND attribute_not_exists(#body_storage) '
      'AND #native = :source_native AND #end = :source_end'
    ):
      return (
        item is not None
        and item.get(self._field('#harness', operation)) == values[':bro']
        and self._field('#body_storage', operation) not in item
        and item.get(self._field('#native', operation)) == values[':source_native']
        and item.get(self._field('#end', operation)) == values[':source_end']
      )
    if expression == (
      '#harness = :claude AND attribute_not_exists(#body_storage) '
      'AND #native = :source_native AND #end = :source_end'
    ):
      return (
        item is not None
        and item.get(self._field('#harness', operation)) == values[':claude']
        and self._field('#body_storage', operation) not in item
        and item.get(self._field('#native', operation)) == values[':source_native']
        and item.get(self._field('#end', operation)) == values[':source_end']
      )
    raise AssertionError(f'unsupported condition: {expression}')

  @staticmethod
  def _split_assignments(expression: str) -> list[str]:
    assignments: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(expression):
      if character == '(':
        depth += 1
      elif character == ')':
        depth -= 1
      elif character == ',' and depth == 0:
        assignments.append(expression[start:index].strip())
        start = index + 1
    assignments.append(expression[start:].strip())
    return assignments

  def _apply_update(self, table: str, operation: dict) -> None:
    key = self._read_key(table, operation['Key'])
    item = self.tables[table].get(key)
    if not self._condition(item, operation):
      raise self.exceptions.ConditionalCheckFailedException()
    assert item is not None
    names = self._names(operation)
    values = self._values(operation)
    expression = operation['UpdateExpression'].removeprefix('SET ')
    for assignment in self._split_assignments(expression):
      target, source = (part.strip() for part in assignment.split('=', 1))
      target_parts = [names.get(part, part) for part in target.split('.')]
      field = target_parts[-1]
      if source.startswith('if_not_exists('):
        if field in item:
          continue
        value_name = source.removesuffix(')').split(',')[1].strip()
      else:
        value_name = source
      value = values[value_name]
      if len(target_parts) == 2 and target_parts[0] == 'native':
        item['native'][field] = value
      else:
        item[field] = value

  def put_item(self, *, TableName, Item, **operation):
    item = _deserialize(Item)
    key = self._key(TableName, item)
    current = self.tables[TableName].get(key)
    if not self._condition(current, operation):
      raise self.exceptions.ConditionalCheckFailedException()
    self.tables[TableName][key] = item

  def delete_item(self, *, TableName, Key, **_):
    self.tables[TableName].pop(self._read_key(TableName, Key), None)

  def get_item(self, *, TableName, Key, **_):
    item = self.tables[TableName].get(self._read_key(TableName, Key))
    return {'Item': _serialize(item)} if item is not None else {}

  def update_item(self, *, TableName, **operation):
    self._apply_update(TableName, operation)
    return {}

  def batch_write_item(self, *, RequestItems):
    for table, requests in RequestItems.items():
      for request in requests:
        operation = request['PutRequest']
        self.put_item(TableName=table, Item=operation['Item'])
    return {'UnprocessedItems': {}}

  def transact_write_items(self, *, TransactItems):
    snapshot = copy.deepcopy(self.tables)
    codes: list[str] = []
    failed = False
    for transaction_item in TransactItems:
      name, operation = next(iter(transaction_item.items()))
      try:
        if name == 'Put':
          self.put_item(
            TableName=operation['TableName'],
            Item=operation['Item'],
            ConditionExpression=operation.get('ConditionExpression'),
            ExpressionAttributeNames=operation.get('ExpressionAttributeNames'),
            ExpressionAttributeValues=operation.get('ExpressionAttributeValues'),
          )
        elif name == 'Update':
          self._apply_update(operation['TableName'], operation)
        else:
          raise AssertionError(name)
      except self.exceptions.ConditionalCheckFailedException:
        codes.append('ConditionalCheckFailed')
        failed = True
      else:
        codes.append('None')
    if failed:
      self.tables = snapshot
      exception = self.exceptions.TransactionCanceledException('cancelled')
      exception.response = {'CancellationReasons': [{'Code': code} for code in codes]}  # type: ignore[attr-defined]
      raise exception

  def query(self, **kwargs):
    self.queries.append(kwargs)
    self.query_threads.append(threading.get_ident())
    table = kwargs['TableName']
    values = self._values(kwargs)
    if 'IndexName' in kwargs:
      partition_name = {
        'all-index': 'gsi_pk',
        'bro-started_at-index': 'bro',
        'harness-started_at-index': 'harness',
        'forked-from-id-index': 'forked_from_id',
        'uuid-index': 'uuid',
      }[kwargs['IndexName']]
      partition_value = values.get(':partition', values.get(':uuid'))
      items = [
        item for item in self.tables[table].values() if item.get(partition_name) == partition_value
      ]
      if ':since' in values:
        items = [item for item in items if item['started_at'] >= values[':since']]
      if ':until' in values:
        items = [item for item in items if item['started_at'] <= values[':until']]
      if kwargs['IndexName'] == 'uuid-index':
        items.sort(key=lambda item: (item['trail_id'], item['step_id']))
      else:
        items.sort(key=lambda item: item['started_at'], reverse=True)
    else:
      trail_id = values[':trail_id']
      items = [item for item in self.tables[table].values() if item['trail_id'] == trail_id]
      if 'BETWEEN' in kwargs['KeyConditionExpression']:
        items = [item for item in items if values[':start'] <= item['step_id'] <= values[':end']]
      elif '<=' in kwargs['KeyConditionExpression']:
        items = [item for item in items if item['step_id'] <= values[':through']]
      items.sort(key=lambda item: item['step_id'])
      start_key = kwargs.get('ExclusiveStartKey')
      if start_key is not None:
        after = _deserialize(start_key)['step_id']
        items = [item for item in items if item['step_id'] > after]
    limit = kwargs.get('Limit')
    page = items if limit is None else items[:limit]
    response: dict[str, Any] = {'Items': [_serialize(item) for item in page]}
    if limit is not None and len(items) > limit:
      last = page[-1]
      if table == 'headers':
        response['LastEvaluatedKey'] = _serialize({'id': last['id']})
      else:
        response['LastEvaluatedKey'] = _serialize(
          {'trail_id': last['trail_id'], 'step_id': last['step_id']}
        )
    return response

  def scan(self, *, TableName, **kwargs):
    self.scans.append({'TableName': TableName, **kwargs})
    items = list(self.tables[TableName].values())
    if 'FilterExpression' in kwargs:
      values = self._values(kwargs)
      requested = set(values.values())
      items = [item for item in items if item.get('uuid') in requested]
    return {'Items': [_serialize(item) for item in items]}


@pytest.fixture
def components():
  dynamo_client = FakeDynamo()
  s3 = FakeS3()
  store = dynamo_store.DynamoStore(
    dynamo=dynamo_client,
    s3=s3,
    trails_table='headers',
    steps_table='universal',
    bucket='bucket',
    uuid_index='uuid-index',
  )
  with store:
    yield store, dynamo_client, s3


def _blaze_bro(store: dynamo_store.DynamoStore, **overrides) -> str:
  payload = {
    'harness': 'bro',
    'version': '2',
    'bro': 'dev',
    'interactive': False,
    'surface': 'ask',
    'hold': 'unattended',
    'native': {'llm': {'type': 'openai', 'model': 'gpt-5'}},
    'body': {'records': [{'kind': 'system_prompt', 'body': 'prompt', 'turn_index': 0}]},
  }
  payload.update(overrides)
  return (store.blaze(BlazeRequest.from_wire(payload)))['id']


def _blaze_claude(store: dynamo_store.DynamoStore, **overrides) -> str:
  payload = {
    'harness': 'claude',
    'version': '2',
    'interactive': True,
    'surface': 'cw',
    'native': {
      'llm': {'type': 'claude'},
      'segment': 'segment',
      'cw_command': 'cw ss',
      'harness_version': 'unknown',
    },
    'body': {'records': []},
  }
  payload.update(overrides)
  return (store.blaze(BlazeRequest.from_wire(payload)))['id']


def test_build_dynamo_store_uses_the_shared_credential_shape(monkeypatch):
  clients = {'dynamodb': FakeDynamo(), 's3': FakeS3()}
  regions = []

  class FakeSession:
    def __init__(self, *, region_name):
      regions.append(region_name)

    def client(self, name):
      return clients[name]

  monkeypatch.setattr(dynamo_store.boto3, 'Session', FakeSession)
  config = {
    'backend': 'dynamo',
    'trails_table': 'headers',
    'steps_table': 'steps',
    'uuid_index': 'uuid-index',
    'bucket': 'spill',
    'region': 'eu-test-1',
  }
  with dynamo_store.build_dynamo_store(config) as store:
    assert isinstance(store, dynamo_store.DynamoStore)
    assert store._dynamo is clients['dynamodb']
    assert store._s3 is clients['s3']
  assert regions == ['eu-test-1']


def test_build_dynamo_store_requires_every_backend_field():
  with pytest.raises(ValueError, match='missing fields'):
    dynamo_store.build_dynamo_store({'backend': 'dynamo'})


def _bro_call(output: list[dict], *, model: str = 'gpt-5', input_tokens: int = 10) -> dict:
  return {
    'kind': 'llm_call',
    'body': {
      'request': {},
      'response': {
        'model': model,
        'usage': {'input_tokens': input_tokens, 'output_tokens': 3},
        'output': output,
      },
    },
    'response_id': 'response-1',
  }


def _claude_assistant(message_id: str, text: str, *, uuid: str) -> str:
  return json.dumps(
    {
      'type': 'assistant',
      'uuid': uuid,
      'timestamp': '2026-01-01T00:00:00Z',
      'message': {
        'id': message_id,
        'model': 'claude-opus',
        'usage': {'input_tokens': 7, 'output_tokens': 2},
        'content': [{'type': 'text', 'text': text}],
      },
    }
  )


def test_five_function_registry_and_declared_projection_contract():
  assert 'end' not in MESSAGE_TYPES
  assert set(backends.BACKENDS) == {'bro', 'claude'}
  for adapter in backends.BACKENDS.values():
    assert {
      name
      for name in ('parse', 'classify', 'project', 'open', 'validate_create')
      if callable(getattr(adapter, name))
    } == {'parse', 'classify', 'project', 'open', 'validate_create'}
    assert adapter.emitted_message_types <= MESSAGE_TYPES


def test_universal_append_uses_ordinals_folds_raw_usage_and_is_idempotent(components):
  store, dynamo, _ = components
  trail_id = _blaze_bro(store)
  result = store.append_records(
    trail_id,
    offset=1,
    records=[
      {'kind': 'user_input', 'body': 'hello'},
      _bro_call([{'type': 'message', 'content': [{'type': 'output_text', 'text': 'hi'}]}]),
    ],
  )
  assert result == {'extent': 3, 'appended': 2}
  assert sorted(
    step_id for row_trail, step_id in dynamo.universal_steps if row_trail == trail_id
  ) == [
    0,
    1,
    2,
  ]
  header = dynamo.headers[trail_id]
  assert header['extent'] == 3
  assert header['turn_count'] == 1
  assert header['native']['usage'] == {'gpt-5': {'input_tokens': 10, 'output_tokens': 3}}
  assert (store.get_trail(trail_id))['usage'] == header['native']['usage']
  assert (store.check(trail_id))['ok'] is True

  duplicate = store.append_records(
    trail_id,
    offset=1,
    records=[
      {'kind': 'user_input', 'body': 'hello'},
      _bro_call([{'type': 'message', 'content': [{'type': 'output_text', 'text': 'hi'}]}]),
    ],
  )
  assert duplicate == {'extent': 3, 'appended': 0, 'duplicate': True}
  assert dynamo.headers[trail_id]['turn_count'] == 1
  with pytest.raises(AppendConflict):
    store.append_records(
      trail_id,
      offset=1,
      records=[
        {'kind': 'user_input', 'body': 'different'},
        {'kind': 'error', 'body': 'competing writer'},
      ],
    )
  with pytest.raises(AppendConflict) as caught:
    store.append_records(trail_id, offset=0, records=[{'kind': 'error', 'body': 'x'}])
  assert caught.value.actual == 3


def test_append_chunks_without_interleaving(components):
  store, dynamo, _ = components
  trail_id = _blaze_bro(store)
  records = [{'kind': 'error', 'body': str(index)} for index in range(51)]
  assert store.append_records(trail_id, offset=1, records=records) == {
    'extent': 52,
    'appended': 51,
  }
  assert dynamo.headers[trail_id]['extent'] == 52


def test_bro_projection_derives_output_and_skips_decomposed_rows(components):
  store, _, _ = components
  trail_id = _blaze_bro(store)
  store.append_records(
    trail_id,
    offset=1,
    records=[
      _bro_call(
        [
          {
            'type': 'reasoning',
            'summary': [{'type': 'summary_text', 'text': 'think'}],
          },
          {'type': 'function_call', 'name': 'read', 'call_id': 'call-1', 'arguments': '{}'},
        ]
      ),
      {'kind': 'reasoning', 'body': 'duplicate'},
      {'kind': 'tool_call', 'body': None, 'call_id': 'call-1'},
    ],
  )
  page = store.get_messages(trail_id, after=None, limit=20, types=None)
  assert [message['type'] for message in page['messages']] == [
    'system_prompt',
    'llm_call',
    'reasoning',
    'tool_call',
  ]
  assert page['messages'][2]['source'] == {'step_id': 1, 'index': 1}
  assert page['messages'][3]['source'] == {'step_id': 1, 'index': 2}

  store.append_records(
    trail_id,
    offset=4,
    records=[
      _bro_call([{'type': 'message', 'content': [{'type': 'output_text', 'text': 'done'}]}])
    ],
  )
  messages = (store.get_messages(trail_id, after=3, limit=10, types=None))['messages']
  assistant = next(message for message in messages if message['type'] == 'assistant')
  assert assistant['terminal'] is True


def test_claude_billing_decision_is_stored_across_batches_and_pages(components):
  store, dynamo, _ = components
  trail_id = _blaze_claude(store)
  first = _claude_assistant('message-1', 'first', uuid='uuid-1')
  second = _claude_assistant('message-1', 'second', uuid='uuid-2')
  third = _claude_assistant('message-1', 'third', uuid='uuid-3')
  store.append_records(trail_id, offset=0, records=[first, second])
  store.append_records(trail_id, offset=2, records=[third])
  assert dynamo.universal_steps[(trail_id, 0)]['usage'] == {
    'input_tokens': 7,
    'output_tokens': 2,
  }
  assert 'usage' not in dynamo.universal_steps[(trail_id, 1)]
  assert 'usage' not in dynamo.universal_steps[(trail_id, 2)]
  assert dynamo.headers[trail_id]['native']['usage']['claude-opus']['input_tokens'] == 7

  billed = []
  after: Optional[int] = None
  while True:
    page = store.get_messages(trail_id, after=after, limit=1, types=None)
    billed.extend(message for message in page['messages'] if message['type'] == 'llm_call')
    after = page['next']
    if after is None:
      break
  assert len(billed) == 1
  assert (store.check(trail_id))['ok'] is True


def test_check_detects_non_adjacent_message_billing(components):
  store, _, _ = components
  trail_id = _blaze_claude(store)
  store.append_records(
    trail_id,
    offset=0,
    records=[_claude_assistant('message-a', 'first', uuid='uuid-1')],
  )
  store.append_records(
    trail_id,
    offset=1,
    records=[_claude_assistant('message-b', 'middle', uuid='uuid-2')],
  )
  store.append_records(
    trail_id,
    offset=2,
    records=[_claude_assistant('message-a', 'last', uuid='uuid-3')],
  )
  checked = store.check(trail_id)
  differences = checked['trails'][0]['differences']
  assert checked['ok'] is False
  assert any(
    difference.get('message_id') == 'message-a' and difference['field'] == 'billing_contributions'
    for difference in differences
  )


def test_claude_classifier_owns_version_title_and_turns(components):
  store, dynamo, _ = components
  trail_id = _blaze_claude(store)
  records = [
    json.dumps({'type': 'system', 'version': '2.1.0'}),
    json.dumps({'type': 'ai-title', 'aiTitle': 'A useful title'}),
    json.dumps({'type': 'user', 'message': {'content': 'hello'}}),
    json.dumps(
      {
        'type': 'user',
        'message': {
          'content': [{'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'result'}]
        },
      }
    ),
  ]
  store.append_records(trail_id, offset=0, records=records)
  header = dynamo.headers[trail_id]
  assert header['native']['harness_version'] == '2.1.0'
  assert header['subject'] == 'A useful title'
  assert header['turn_count'] == 1


def test_spill_and_content_addressed_tools(components):
  store, _, s3 = components
  trail_id = _blaze_bro(store)
  tool_body = [{'type': 'function', 'name': 'read'}]
  sha256 = tools_sha256(tool_body)
  large = 'x' * (dynamo_store.SPILLOVER_THRESHOLD_BYTES + 1)
  store.append_records(
    trail_id,
    offset=1,
    records=[{'kind': 'tool_result', 'body': large, 'tools_sha256': sha256}],
    tools={sha256: tool_body},
  )
  store.append_records(trail_id, offset=2, records=[], tools={sha256: tool_body})
  tool_key = dynamo_types.tool_blob_key(sha256)
  assert s3.put_counts[tool_key] == 1
  row = next(row for row in s3.objects if row.startswith(f'trails/steps/{trail_id}/1-'))
  assert row in s3.objects
  assert (store.get_steps(trail_id, after=None, limit=10))['steps'][1]['body'] == large
  with pytest.raises(ValueError, match='hash mismatch'):
    store.append_records(trail_id, offset=2, records=[], tools={'0' * 64: tool_body})


def test_spilled_claude_lines_resolve_in_the_store_thread_pool(components):
  store, _, s3 = components
  trail_id = _blaze_claude(store)
  raw_lines = [
    json.dumps(
      {
        'type': 'system',
        'content': character * (dynamo_store.SPILLOVER_THRESHOLD_BYTES + 1),
      }
    )
    for character in ('x', 'y')
  ]
  store.append_records(trail_id, offset=0, records=raw_lines)
  caller_thread = threading.get_ident()
  steps = store.get_steps(trail_id, after=None, limit=10)['steps']

  assert [step['body'] for step in steps] == raw_lines
  assert all(thread != caller_thread for thread in s3.get_threads)
  assert all('raw' not in step and 'record' not in step for step in steps)


def test_uuid_projection_and_point_reads(components):
  store, dynamo, _ = components
  universal = _blaze_claude(store)
  first = _claude_assistant('message-1', 'first', uuid='uuid-1')
  second = _claude_assistant('message-2', 'second', uuid='uuid-2')
  store.append_records(universal, offset=0, records=[first, second])

  query_count = len(dynamo.query_threads)
  caller_thread = threading.get_ident()
  [match] = store.find_segment_steps({'segment'}, {'uuid-2', 'missing'})
  assert (match['trail_id'], match['step_id'], match['uuid']) == (universal, 1, 'uuid-2')
  assert match['header']['native']['segment'] == 'segment'
  assert store.find_segment_steps({'other-segment'}, {'uuid-2'}) == []
  assert all(thread != caller_thread for thread in dynamo.query_threads[query_count:])
  assert dynamo.queries[-1]['IndexName'] == 'uuid-index'
  assert dynamo.queries[-1]['ProjectionExpression'] == 'trail_id, step_id, #uuid'
  assert (store.get_step(universal, 1))['body'] == second
  assert store.get_step_uuids(universal, through=0) == [{'step_id': 0, 'uuid': 'uuid-1'}]


def test_launch_context_is_harness_neutral_and_end_adds_no_step(components):
  store, dynamo, s3 = components
  trail_id = _blaze_bro(
    store,
    body={
      'records': [{'kind': 'system_prompt', 'body': 'prompt'}],
      'launch_context': {'cwd': '/workspace'},
    },
  )
  header = dynamo.headers[trail_id]
  assert header['context_s3'] == dynamo_types.context_key(trail_id)
  assert 'context_s3' not in header['native']
  assert store.get_launch_context(trail_id) == {'cwd': '/workspace'}
  assert dynamo_types.context_key(trail_id) in s3.objects
  before = len(dynamo.universal_steps)
  store.end_trail(trail_id=trail_id, reason='ok', detail=None)
  assert len(dynamo.universal_steps) == before
  assert dynamo.headers[trail_id]['end']['reason'] == 'ok'


def test_check_detects_corruption_and_recompute_repairs_rows_and_header(components):
  store, dynamo, _ = components
  trail_id = _blaze_claude(store)
  store.append_records(
    trail_id,
    offset=0,
    records=[_claude_assistant('message-1', 'first', uuid='uuid-1')],
  )
  dynamo.headers[trail_id]['turn_count'] = 9
  dynamo.headers[trail_id]['native']['usage'] = {}
  dynamo.universal_steps[(trail_id, 0)]['uuid'] = 'wrong'
  dynamo.universal_steps[(trail_id, 0)].pop('usage')
  checked = store.check(trail_id)
  assert checked['ok'] is False
  fields = {difference['field'] for difference in checked['trails'][0]['differences']}
  assert {'turn_count', 'native.usage', 'uuid', 'usage', 'billing_contributions'} <= fields

  store.recompute(trail_id)
  assert (store.check(trail_id))['ok'] is True
  assert dynamo.universal_steps[(trail_id, 0)]['uuid'] == 'uuid-1'


def test_store_check_finds_cross_trail_duplicate_uuids(components):
  store, _, _ = components
  first = _blaze_claude(store)
  second = _blaze_claude(store)
  store.append_records(
    first,
    offset=0,
    records=[_claude_assistant('message-1', 'first', uuid='duplicate')],
  )
  store.append_records(
    second,
    offset=0,
    records=[_claude_assistant('message-2', 'second', uuid='duplicate')],
  )
  checked = store.check()
  assert checked['ok'] is False
  assert checked['cross_trail_duplicate_uuids'] == [
    {'uuid': 'duplicate', 'trail_ids': sorted([first, second])}
  ]


def test_relink_manifests_before_trimming_and_recomputes(components):
  store, dynamo, s3 = components
  trail_id = _blaze_claude(store)
  records = [
    json.dumps({'type': 'system', 'uuid': 'copied'}),
    json.dumps(
      {
        'type': 'user',
        'uuid': 'own',
        'message': {'content': 'hello'},
      }
    ),
  ]
  store.append_records(trail_id, offset=0, records=records)
  result = store.relink(
    trail_id,
    {'trail_id': 'parent', 'step_id': 7, 'index': 2},
    1,
  )
  assert result['extent'] == 1
  assert result['manifest_s3'] in s3.objects
  manifest = json.loads(s3.objects[result['manifest_s3']])
  assert manifest['deleted_rows'][0]['uuid'] == 'copied'
  assert dynamo.headers[trail_id]['forked_from_id'] == 'parent'
  assert dynamo.headers[trail_id]['turn_count'] == 1
  assert dynamo.universal_steps[(trail_id, 0)]['uuid'] == 'own'
  assert (trail_id, 1) not in dynamo.universal_steps


def test_repair_llm_spec_manifests_the_value_it_replaces(components):
  store, dynamo, s3 = components
  trail_id = _blaze_bro(store)
  recorded = dynamo.headers[trail_id]['native']['llm']

  result = store.repair_llm_spec(trail_id, recorded, {**recorded, 'type': 'anthropic'})

  assert dynamo.headers[trail_id]['native']['llm']['type'] == 'anthropic'
  assert json.loads(s3.objects[result['manifest_s3']])['previous'] == recorded


def test_repair_llm_spec_refuses_a_value_it_did_not_read(components):
  store, dynamo, _ = components
  trail_id = _blaze_bro(store)
  recorded = dynamo.headers[trail_id]['native']['llm']

  with pytest.raises(ValueError, match='not the expected value'):
    store.repair_llm_spec(trail_id, {'type': 'stale'}, {'type': 'anthropic'})
  assert dynamo.headers[trail_id]['native']['llm'] == recorded


def test_repair_llm_spec_leaves_the_rest_of_native_alone(components):
  # `native` also carries the usage an append folds in, so the repair must
  # replace the recipe rather than the record around it
  store, dynamo, _ = components
  trail_id = _blaze_bro(store)
  recorded = dynamo.headers[trail_id]['native']['llm']
  dynamo.headers[trail_id]['native']['usage'] = {'gpt-5': {'input_tokens': 7}}

  store.repair_llm_spec(trail_id, recorded, {**recorded, 'type': 'anthropic'})

  assert dynamo.headers[trail_id]['native']['usage'] == {'gpt-5': {'input_tokens': 7}}


def test_list_and_pointer_index_stay_available(components):
  store, _, _ = components
  root = _blaze_bro(store)
  child = _blaze_bro(
    store,
    forked_from={'trail_id': root, 'step_id': 0, 'index': 2},
  )
  page = store.list_trails(
    harness=None,
    bro=None,
    forked_from=root,
    since=None,
    until=None,
    cursor=None,
    limit=10,
  )
  assert [trail['id'] for trail in page['trails']] == [child]


def test_sweep_marks_stale_trails_as_inferred_unreported(components):
  store, dynamo, _ = components
  stale = _blaze_bro(store)
  live = _blaze_bro(store)
  dynamo.headers[stale]['last_alive_at'] = '2020-01-01T00:00:00.000000Z'

  assert store.sweep_unreported() == [stale]
  assert dynamo.headers[stale]['end'] == {
    'at': '2020-01-01T00:00:00.000000Z',
    'inference': UNREPORTED_END_INFERENCE,
  }
  assert dynamo.headers[live]['end'] is None
