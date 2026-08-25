import json
import threading
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Optional

import boto3
import pytest
from moto import mock_aws

from bro.trails import backends
from bro.trails.model import (
  MESSAGE_TYPES,
  UNREPORTED_END_INFERENCE,
  BlazeRequest,
  payload_sha256,
  tools_sha256,
)
from bro.trails.server import dynamo as dynamo_store, dynamo_types
from bro.trails.store import AppendConflict, TrailHasForks, TrailNotFound

# moto validates the region against the real partition, so the fixture names one
_REGION = 'eu-west-1'
_TRAILS_TABLE = 'headers'
_STEPS_TABLE = 'steps'
_UUID_INDEX = dynamo_store.UUID_INDEX
_BUCKET = 'bucket'


def _attribute_type(attribute: dynamo_store.DynamoAttribute) -> str:
  return {'string': 'S', 'number': 'N'}[attribute.type]


def _key_schema(
  partition_key: dynamo_store.DynamoAttribute,
  sort_key: Optional[dynamo_store.DynamoAttribute],
) -> list[dict]:
  keys = [{'AttributeName': partition_key.name, 'KeyType': 'HASH'}]
  if sort_key is not None:
    keys.append({'AttributeName': sort_key.name, 'KeyType': 'RANGE'})
  return keys


def _create_table(dynamo, name: str, schema: dynamo_store.DynamoTable) -> None:
  attributes = {
    attribute.name: attribute
    for index in schema.indexes
    for attribute in (index.partition_key, index.sort_key)
    if attribute is not None
  }
  attributes[schema.partition_key.name] = schema.partition_key
  if schema.sort_key is not None:
    attributes[schema.sort_key.name] = schema.sort_key
  dynamo.create_table(
    TableName=name,
    KeySchema=_key_schema(schema.partition_key, schema.sort_key),
    AttributeDefinitions=[
      {'AttributeName': attribute.name, 'AttributeType': _attribute_type(attribute)}
      for attribute in attributes.values()
    ],
    GlobalSecondaryIndexes=[
      {
        'IndexName': index.name,
        'KeySchema': _key_schema(index.partition_key, index.sort_key),
        'Projection': {'ProjectionType': index.projection.upper()},
      }
      for index in schema.indexes
    ],
    BillingMode='PAY_PER_REQUEST',
  )


def _create_tables(dynamo) -> None:
  _create_table(dynamo, _TRAILS_TABLE, dynamo_store.TRAILS_TABLE)
  _create_table(dynamo, _STEPS_TABLE, dynamo_store.STEPS_TABLE)


class _Items(Mapping):
  """One table's items, read live and keyed by the table's key attributes."""

  def __init__(self, dynamo, table: str, key_names: tuple[str, ...]):
    self._dynamo = dynamo
    self._table = table
    self._key_names = key_names

  def __getitem__(self, key) -> dict:
    response = self._dynamo.get_item(
      TableName=self._table, Key=dynamo_types.ddb_item(self._key(key)), ConsistentRead=True
    )
    item = dynamo_types.from_ddb_item(response.get('Item'))
    if item is None:
      raise KeyError(key)
    return item

  def __iter__(self) -> Iterator:
    names = {f'#key{index}': name for index, name in enumerate(self._key_names)}
    pages = self._dynamo.get_paginator('scan').paginate(
      TableName=self._table,
      ProjectionExpression=', '.join(names),
      ExpressionAttributeNames=names,
    )
    for page in pages:
      for raw in page['Items']:
        item = dynamo_types.from_ddb_item(raw)
        assert item is not None
        values = tuple(item[name] for name in self._key_names)
        yield values[0] if len(values) == 1 else values

  def __len__(self) -> int:
    return sum(1 for _ in iter(self))

  def _key(self, key) -> dict:
    values = key if isinstance(key, tuple) else (key,)
    return dict(zip(self._key_names, values, strict=True))


class Tables:
  """The stored items keyed the way the tests name them, plus the queries the store issued."""

  def __init__(self, dynamo):
    self._dynamo = dynamo
    self.queries: list[dict] = []
    self.headers = _Items(dynamo, _TRAILS_TABLE, ('id',))
    self.steps = _Items(dynamo, _STEPS_TABLE, ('trail_id', 'step_id'))
    dynamo.meta.events.register(
      'provide-client-params.dynamodb.Query',
      lambda params, **_: self.queries.append(params),
    )

  @contextmanager
  def editing_header(self, trail_id: str) -> Iterator[dict]:
    yield from self._editing(_TRAILS_TABLE, self.headers[trail_id])

  @contextmanager
  def editing_step(self, trail_id: str, step_id: int) -> Iterator[dict]:
    yield from self._editing(_STEPS_TABLE, self.steps[trail_id, step_id])

  def _editing(self, table: str, item: dict) -> Iterator[dict]:
    yield item
    self._dynamo.put_item(TableName=table, Item=dynamo_types.ddb_item(item))


class _Objects(Mapping[str, bytes]):
  def __init__(self, s3):
    self._s3 = s3

  def __contains__(self, key: object) -> bool:
    return key in self._keys()

  def __iter__(self) -> Iterator[str]:
    return iter(self._keys())

  def __len__(self) -> int:
    return len(self._keys())

  def __getitem__(self, key: str) -> bytes:
    return self._s3.get_object(Bucket=_BUCKET, Key=key)['Body'].read()

  def _keys(self) -> list[str]:
    pages = self._s3.get_paginator('list_objects_v2').paginate(Bucket=_BUCKET)
    return [stored['Key'] for page in pages for stored in page.get('Contents', [])]


class Bucket:
  """The stored objects, plus the keys the store wrote and the threads its reads ran on."""

  def __init__(self, s3, reader):
    # the view reads through its own client so that browsing objects leaves
    # `get_threads` reporting only what the store itself fetched
    self.objects = _Objects(reader)
    self.put_counts: Counter[str] = Counter()
    self.get_threads: list[int] = []
    s3.meta.events.register(
      'provide-client-params.s3.PutObject',
      lambda params, **_: self.put_counts.update([params['Key']]),
    )
    s3.meta.events.register(
      'provide-client-params.s3.GetObject',
      lambda params, **_: self.get_threads.append(threading.get_ident()),
    )


@pytest.fixture
def components():
  with mock_aws():
    session = boto3.Session(region_name=_REGION)
    dynamo = session.client('dynamodb')
    s3 = session.client('s3')
    _create_tables(dynamo)
    s3.create_bucket(Bucket=_BUCKET, CreateBucketConfiguration={'LocationConstraint': _REGION})
    store = dynamo_store.DynamoStore(
      dynamo=dynamo,
      s3=s3,
      trails_table=_TRAILS_TABLE,
      steps_table=_STEPS_TABLE,
      bucket=_BUCKET,
      uuid_index=_UUID_INDEX,
    )
    with store:
      yield store, Tables(dynamo), Bucket(s3, session.client('s3'))


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


_CLAUDE_NATIVE = {
  'llm': {'type': 'claude'},
  'segment': 'segment',
  'ride_command': 'ride along',
  'harness_version': 'unknown',
}


def _claude_payload(**overrides) -> dict:
  payload = {
    'harness': 'claude',
    'version': '2',
    'interactive': True,
    'surface': 'ride',
    'native': dict(_CLAUDE_NATIVE),
    'body': {'records': []},
  }
  payload.update(overrides)
  return payload


def _blaze_claude(store: dynamo_store.DynamoStore, **overrides) -> str:
  return (store.blaze(BlazeRequest.from_wire(_claude_payload(**overrides))))['id']


def test_build_dynamo_store_uses_the_shared_credential_shape(monkeypatch):
  clients = {'dynamodb': object(), 's3': object()}
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


def test_append_uses_ordinals_folds_raw_usage_and_is_idempotent(components):
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
  assert sorted(step_id for row_trail, step_id in dynamo.steps if row_trail == trail_id) == [
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
  assert dynamo.steps[(trail_id, 0)]['usage'] == {
    'input_tokens': 7,
    'output_tokens': 2,
  }
  assert 'usage' not in dynamo.steps[(trail_id, 1)]
  assert 'usage' not in dynamo.steps[(trail_id, 2)]
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


def test_candidate_trails_come_from_the_segment_index(components):
  store, dynamo, _ = components
  in_segment = _blaze_claude(store)
  elsewhere = _blaze_claude(store, native={**_CLAUDE_NATIVE, 'segment': 'other-segment'})
  first = _claude_assistant('message-1', 'first', uuid='uuid-1')
  store.append_records(in_segment, offset=0, records=[first])

  query_count = len(dynamo.queries)
  [header] = store.find_segment_trails({'segment'})

  assert header['id'] == in_segment
  assert header['native']['segment'] == 'segment'
  assert [found['id'] for found in store.find_segment_trails({'other-segment'})] == [elsewhere]
  assert store.find_segment_trails({'missing-segment'}) == []
  assert {query['IndexName'] for query in dynamo.queries[query_count:]} == {
    dynamo_store.SEGMENT_INDEX
  }
  assert (store.get_step(in_segment, 0))['body'] == first


def test_the_mid_write_probe_is_one_keys_only_uuid_query(components):
  store, dynamo, _ = components
  recorded = _blaze_claude(store)
  elsewhere = _blaze_claude(store)
  store.append_records(
    recorded, offset=0, records=[_claude_assistant('message-1', 'first', uuid='uuid-1')]
  )

  query_count = len(dynamo.queries)
  assert store.holds_record({recorded}, 'uuid-1') is True

  assert store.holds_record({elsewhere}, 'uuid-1') is False
  assert store.holds_record({recorded}, 'missing') is False
  assert store.holds_record(set(), 'uuid-1') is False
  probes = dynamo.queries[query_count:]
  assert len(probes) == 3
  assert {query['IndexName'] for query in probes} == {'uuid-index'}
  assert {query['ProjectionExpression'] for query in probes} == {'trail_id'}


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
  before = len(dynamo.steps)
  store.end_trail(trail_id=trail_id, reason='ok', detail=None)
  assert len(dynamo.steps) == before
  assert dynamo.headers[trail_id]['end']['reason'] == 'ok'


def test_the_segment_key_and_lineage_head_ride_the_header_writes(components):
  store, dynamo, _ = components
  trail_id = _blaze_claude(store)
  first = _claude_assistant('message-1', 'first', uuid='uuid-1')

  assert dynamo.headers[trail_id]['segment'] == 'segment'
  store.append_records(trail_id, offset=0, records=[first])

  assert dynamo.headers[trail_id]['native']['lineage_head'] == {
    'chain_first_uuid': 'uuid-1',
    'tail': [[0, 'uuid-1', payload_sha256(first)]],
    'last_row_digest': payload_sha256(first),
    'cuts': None,
  }
  assert 'segment' not in dynamo.headers[_blaze_bro(store)]


def test_attaching_reopens_the_trail_conditional_on_the_extent_it_verified(components, monkeypatch):
  store, dynamo, _ = components
  trail_id = _blaze_claude(store)
  recorded = _claude_assistant('message-1', 'first', uuid='uuid-1')
  store.append_records(trail_id, offset=0, records=[recorded])
  store.end_trail(trail_id, 'ok')
  resumed = [
    _claude_assistant(f'message-{index}', 'text', uuid=f'uuid-{index}') for index in (2, 3)
  ]
  lineage = {
    'segment': 'segment',
    'lines': [
      [f'uuid-{index}', payload_sha256(raw)]
      for index, raw in enumerate([recorded, *resumed], start=1)
    ],
  }

  attached = store.blaze(BlazeRequest.from_wire(_claude_payload(lineage=lineage, version='3')))

  assert (attached['id'], attached['extent'], attached['chunks']) == (trail_id, 1, [[1, 1]])
  assert dynamo.headers[trail_id]['end'] is None
  assert dynamo.headers[trail_id]['version'] == '3'
  assert dynamo.headers[trail_id]['native']['lineage_head']['tail'] == [
    [0, 'uuid-1', payload_sha256(recorded)]
  ]

  # the race the extent condition covers: an append lands between the header the
  # verdict was verified against and the write that reopens it
  stale = store.find_segment_trails({'segment'})
  store.append_records(trail_id, offset=1, records=resumed[:1])
  monkeypatch.setattr(store, 'find_segment_trails', lambda segments: stale)

  contended = store.blaze(BlazeRequest.from_wire(_claude_payload(lineage=lineage, version='4')))

  assert contended == {'adopted': False, 'reason': backends.ATTACH_CONTENDED}
  assert dynamo.headers[trail_id]['version'] == '3'


def test_check_detects_corruption_and_recompute_repairs_rows_and_header(components):
  store, dynamo, _ = components
  trail_id = _blaze_claude(store)
  store.append_records(
    trail_id,
    offset=0,
    records=[_claude_assistant('message-1', 'first', uuid='uuid-1')],
  )
  with dynamo.editing_header(trail_id) as header:
    header['turn_count'] = 9
    header['native']['usage'] = {}
    header['native']['lineage_head']['tail'] = []
  with dynamo.editing_step(trail_id, 0) as step:
    step['uuid'] = 'wrong'
    step.pop('usage')
  checked = store.check(trail_id)
  assert checked['ok'] is False
  fields = {difference['field'] for difference in checked['trails'][0]['differences']}
  assert {
    'turn_count',
    'native.usage',
    'native.lineage_head',
    'uuid',
    'usage',
    'billing_contributions',
  } <= fields

  store.recompute(trail_id)
  assert (store.check(trail_id))['ok'] is True
  assert dynamo.steps[(trail_id, 0)]['uuid'] == 'uuid-1'


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
  # the deleted prefix was the copy of the conversation's opening, so the head
  # keeps the record it named while the window follows the rows that remain
  head = dynamo.headers[trail_id]['native']['lineage_head']
  assert head['chain_first_uuid'] == 'copied'
  assert [row[1] for row in head['tail']] == ['own']
  assert dynamo.steps[(trail_id, 0)]['uuid'] == 'own'
  assert (trail_id, 1) not in dynamo.steps


def test_delete_manifests_the_trail_and_takes_only_what_it_owns(components):
  store, dynamo, s3 = components
  tool_body = [{'type': 'function', 'name': 'read'}]
  sha256 = tools_sha256(tool_body)
  trail_id = _blaze_bro(
    store,
    body={
      'records': [{'kind': 'system_prompt', 'body': 'prompt'}],
      'launch_context': {'cwd': '/workspace'},
    },
  )
  large = 'x' * (dynamo_store.SPILLOVER_THRESHOLD_BYTES + 1)
  store.append_records(
    trail_id,
    offset=1,
    records=[{'kind': 'tool_result', 'body': large, 'tools_sha256': sha256}],
    tools={sha256: tool_body},
  )
  spilled = next(key for key in s3.objects if key.startswith(f'trails/steps/{trail_id}/1-'))

  result = store.delete_trail(trail_id)

  assert result == {'trail_id': trail_id, 'extent': 2, 'manifest': result['manifest']}
  manifest = json.loads(s3.objects[result['manifest']])
  assert manifest['header']['id'] == trail_id
  assert [step['body'] for step in manifest['steps']] == ['prompt', large]
  assert trail_id not in dynamo.headers
  assert [key for key in dynamo.steps if key[0] == trail_id] == []
  assert spilled not in s3.objects
  assert dynamo_types.context_key(trail_id) not in s3.objects
  assert dynamo_types.tool_blob_key(sha256) in s3.objects
  with pytest.raises(TrailNotFound):
    store.delete_trail(trail_id)


def test_delete_refuses_a_trail_a_fork_still_points_at(components):
  store, _, _ = components
  root = _blaze_bro(store)
  child = _blaze_bro(store, forked_from={'trail_id': root, 'step_id': 0})

  with pytest.raises(TrailHasForks) as refused:
    store.delete_trail(root)

  assert refused.value.forks == [child]
  store.delete_trail(child)
  assert store.delete_trail(root)['trail_id'] == root


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
  with dynamo.editing_header(stale) as header:
    header['last_alive_at'] = '2020-01-01T00:00:00.000000Z'

  assert store.sweep_unreported() == [stale]
  assert dynamo.headers[stale]['end'] == {
    'at': '2020-01-01T00:00:00.000000Z',
    'inference': UNREPORTED_END_INFERENCE,
  }
  assert dynamo.headers[live]['end'] is None
