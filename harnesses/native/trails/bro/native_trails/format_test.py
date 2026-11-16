import importlib.metadata
import json

import pytest

from bro.llm import wires
from bro.native_trails.format import BRO_FORMAT
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest, named_tool_digests, tools_sha256
from bro.trails.store import fetch_recorded_trail


def native_request(records):
  return BlazeRequest(
    harness='bro',
    bro='bro',
    version='test',
    interactive=False,
    surface='test',
    native={'llm': {'type': 'openai', 'model': 'model'}},
    body={'records': records},
  )


def test_tagged_and_untagged_responses_project_identically_through_a_store(tmp_path):
  response = {
    'model': 'model',
    'status': 'completed',
    'service_tier': 'priority',
    'usage': {'input_tokens': 10, 'output_tokens': 2},
    'output': [
      {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'think'}]},
      {'type': 'message', 'content': [{'type': 'output_text', 'text': 'reply'}]},
    ],
  }
  with LocalStore(tmp_path) as store:
    projected = []
    for extras in ({}, {'wire': 'responses'}):
      trail = store.blaze(
        native_request(
          [
            {
              'kind': 'llm_call',
              'ts': '2026-01-01T00:00:00Z',
              'body': {'request': {}, 'response': response},
              **extras,
            }
          ]
        )
      )
      projected.append(list(store.iter_messages(trail['id'])))
  assert projected[0] == projected[1]
  assert [(event['type'], event['source']['index']) for event in projected[0]] == [
    ('llm_call', 0),
    ('reasoning', 1),
    ('assistant', 2),
  ]
  assert projected[0][0]['usage'] == response['usage']
  assert projected[0][0]['service_tier'] == 'priority'
  assert projected[0][2]['terminal'] is True


def test_chat_projection_preserves_calls_interim_text_and_stop(tmp_path):
  response = {
    'model': 'deepseek-v4-pro',
    'usage': {'prompt_tokens': 10, 'completion_tokens': 3},
    'choices': [
      {
        'finish_reason': 'tool_calls',
        'message': {
          'reasoning_content': 'think',
          'content': 'checking',
          'tool_calls': [
            {'id': 'c1', 'type': 'function', 'function': {'name': 'read', 'arguments': ''}}
          ],
        },
      }
    ],
  }
  with LocalStore(tmp_path) as store:
    trail = store.blaze(
      native_request(
        [{'kind': 'llm_call', 'wire': 'chat', 'body': {'request': {}, 'response': response}}]
      )
    )
    messages = list(store.iter_messages(trail['id']))
    assert [event['type'] for event in messages] == [
      'llm_call',
      'reasoning',
      'assistant',
      'tool_call',
    ]
    assert messages[2]['terminal'] is False
    assert messages[3]['source'] == {'step_id': 0, 'index': 3}
    assert messages[3]['arguments'] == {}
    assert messages[3]['call_id'] == 'c1'
    response['choices'][0]['finish_reason'] = 'length'
    response['choices'][0]['message'] = {'content': 'partial'}
    store.append_records(
      trail['id'],
      offset=1,
      records=[{'kind': 'llm_call', 'wire': 'chat', 'body': {'request': {}, 'response': response}}],
    )
    stopped = list(store.iter_messages(trail['id'], after=0))
    assert stopped[0]['stop'] == 'truncated'
    assert stopped[1]['terminal'] is False


@pytest.mark.parametrize('wire', ['missing', None])
def test_unavailable_wire_keeps_the_whole_record_as_one_event(tmp_path, monkeypatch, wire):
  monkeypatch.setattr(wires, 'registry', wires.WireRegistry())
  monkeypatch.setattr(wires, '_entry_points', lambda: ())
  record = {
    'kind': 'llm_call',
    'body': {
      'request': {},
      'response': {'model': 'model', 'usage': {'input_tokens': 10}, 'output': []},
    },
  }
  if wire is not None:
    record['wire'] = wire
  with LocalStore(tmp_path) as store:
    trail = store.blaze(native_request([record]))
    messages = list(store.iter_messages(trail['id']))
    rows = list(store.iter_steps(trail['id']))
  assert len(messages) == 1
  assert messages[0]['type'] == 'harness_event'
  assert messages[0]['raw'] == rows[0]
  assert rows[0]['body'] == record['body']


def test_broken_installed_wire_fails_loudly(tmp_path, monkeypatch):
  monkeypatch.setattr(wires, 'registry', wires.WireRegistry())
  monkeypatch.setattr(
    wires,
    '_entry_points',
    lambda: (importlib.metadata.EntryPoint('broken', 'missing.module', wires.ENTRY_POINT_GROUP),),
  )
  with LocalStore(tmp_path) as store:
    trail = store.blaze(
      native_request([{'kind': 'llm_call', 'wire': 'broken', 'body': {'response': {}}}])
    )
    with pytest.raises(RuntimeError, match='failed to load wire'):
      list(store.iter_messages(trail['id']))


@pytest.fixture
def step_records():
  return [
    {'kind': 'system_prompt', 'body': 'system', 'turn_index': 0},
    {'kind': 'user_input', 'body': 'hello', 'turn_index': 0},
    {
      'kind': 'notification',
      'body': 'news',
      'turn_index': 0,
      'call_index': 1,
      'job_ids': ['job'],
    },
    {
      'kind': 'llm_call',
      'body': {
        'request': {'model': 'model'},
        'response': {
          'model': 'model',
          'status': 'completed',
          'usage': {'input_tokens': 10, 'output_tokens': 2},
          'output': [
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'think'}]},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'checking'}]},
            {'type': 'function_call', 'name': 'read', 'call_id': 'c1', 'arguments': '{}'},
          ],
        },
      },
      'wire': 'responses',
      'tools_sha256': tools_sha256([]),
      'turn_index': 0,
      'call_index': 1,
      'response_id': 'response-1',
      'stopped': 'truncated',
    },
    {
      'kind': 'tool_result',
      'body': {'value': 'result'},
      'turn_index': 0,
      'call_index': 1,
      'tool_name': 'read',
      'call_id': 'c1',
      'is_error': False,
    },
    {'kind': 'error', 'body': {'message': 'failed', 'traceback': 'trace'}},
  ]


def line_record(record):
  return {
    'run_log': 1,
    'body': json.dumps({'at': '2026-11-14T21:00:00.000000Z', **record}, ensure_ascii=False),
  }


def test_line_rows_store_verbatim_and_read_like_spread_rows(tmp_path, step_records):
  line_records = [line_record(record) for record in step_records]
  with LocalStore(tmp_path) as store:
    trails = []
    for records in (
      [
        {**record, 'ts': json.loads(line['body'])['at']}
        for record, line in zip(step_records, line_records, strict=True)
      ],
      line_records,
    ):
      trail_id = store.blaze(native_request([]))['id']
      store.append_records(trail_id, 0, records, tools={tools_sha256([]): []})
      trails.append(trail_id)
    spread_rows, line_rows = [list(store.iter_steps(trail_id)) for trail_id in trails]
    for spread, row, record in zip(spread_rows, line_rows, line_records, strict=True):
      assert row['body'] == record['body']
      assert row['run_log'] == record['run_log']
      assert row['kind'] == json.loads(record['body'])['kind']
      assert row['ts'] == json.loads(record['body'])['at']
      assert set(row) == {
        'trail_id',
        'format',
        'step_id',
        'ts',
        'kind',
        'payload_sha256',
        'body',
        'run_log',
        *({'tools_sha256', 'usage'} if row['kind'] == 'llm_call' else set()),
      }
      normalized = BRO_FORMAT.native_record(row)
      expected = {**spread, 'trail_id': row['trail_id'], 'payload_sha256': row['payload_sha256']}
      assert normalized == expected
      assert BRO_FORMAT.native_record(spread) is spread
      assert BRO_FORMAT.classify(BRO_FORMAT.parse(row)) == BRO_FORMAT.classify(
        BRO_FORMAT.parse(spread)
      )
      assert BRO_FORMAT.project(row) == BRO_FORMAT.project(spread)
    headers = [store.get_trail(trail_id) for trail_id in trails]
    assert headers[0]['native'] == headers[1]['native']
    assert headers[0]['turn_count'] == headers[1]['turn_count'] == 1
    assert headers[1]['native']['usage'] == {'model': {'input_tokens': 10, 'output_tokens': 2}}
    assert named_tool_digests(line_rows) == {tools_sha256([])}
    recorded = fetch_recorded_trail(store, trails[1])
    assert [step.body for step in recorded.steps] == [record['body'] for record in step_records]
    assert recorded.steps[3].extras['response_id'] == 'response-1'
    assert recorded.steps[3].extras['stopped'] == 'truncated'


@pytest.mark.parametrize('run_log', [None, True, 1.0, '1', 0, 2])
def test_line_rows_refuse_an_unknown_run_log(run_log):
  with pytest.raises(ValueError, match='unsupported bro run_log format'):
    BRO_FORMAT.parse({**line_record({'kind': 'user_input', 'body': 'hello'}), 'run_log': run_log})


@pytest.mark.parametrize('body', [None, {}, [], 1, '', '{', '[]', 'null', '1', '"text"', '{} {}'])
def test_line_rows_refuse_a_body_other_than_one_json_object_string(body):
  with pytest.raises(ValueError, match='body must be a JSON object string'):
    BRO_FORMAT.parse({'run_log': 1, 'body': body})


@pytest.mark.parametrize('kind', [None, [], '', 'open', 'tools', 'end', 'assistant'])
def test_line_rows_refuse_non_step_kinds(kind):
  with pytest.raises(ValueError, match='line kind must be one of'):
    BRO_FORMAT.parse(line_record({'kind': kind}))


@pytest.mark.parametrize('at', [None, 1, {}])
def test_line_rows_require_a_string_timestamp(at):
  with pytest.raises(ValueError, match='line at must be a string'):
    BRO_FORMAT.parse(line_record({'kind': 'user_input', 'body': 'hello', 'at': at}))


@pytest.mark.parametrize('kind', ['system_prompt', 'user_input', 'notification'])
@pytest.mark.parametrize('body', [None, {}, 1])
def test_line_rows_require_text_bodies(kind, body):
  with pytest.raises(ValueError, match=f'{kind} body must be a string'):
    BRO_FORMAT.parse(line_record({'kind': kind, 'body': body}))


@pytest.mark.parametrize('digest', [None, 1, '', 'bad', 'g' * 64, '0' * 63])
def test_line_rows_require_a_tool_digest_on_llm_calls(digest):
  with pytest.raises(ValueError, match='llm_call tools_sha256 must be a SHA-256 digest'):
    BRO_FORMAT.parse(line_record({'kind': 'llm_call', 'body': {}, 'tools_sha256': digest}))


def test_line_rows_require_kind_at_and_tool_digest_fields():
  for step, error in (
    ({'body': 'hello'}, 'line kind'),
    ({'kind': 'user_input', 'body': 'hello'}, 'line at'),
    ({'kind': 'llm_call', 'at': 'time', 'body': {}}, 'tools_sha256'),
  ):
    with pytest.raises(ValueError, match=error):
      BRO_FORMAT.parse({'run_log': 1, 'body': json.dumps(step)})


def test_import_seal_refolds_line_rows_and_preserves_stored_lines(tmp_path, step_records):
  with LocalStore(tmp_path / 'source') as source, LocalStore(tmp_path / 'target') as target:
    trail_id = source.blaze(native_request([]))['id']
    source.append_records(
      trail_id, 0, [line_record(record) for record in step_records], tools={tools_sha256([]): []}
    )
    source.end_trail(trail_id, 'ok')
    header = source.get_trail(trail_id)
    rows = list(source.iter_steps(trail_id))
    target.import_trail(header, rows, tools={tools_sha256([]): []})
    assert target.get_trail(trail_id) == header
    assert list(target.iter_steps(trail_id)) == rows
    assert list(target.iter_messages(trail_id)) == list(source.iter_messages(trail_id))
    assert target.get_tool(tools_sha256([])) == []
