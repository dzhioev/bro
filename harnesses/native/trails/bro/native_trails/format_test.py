import importlib.metadata

import pytest

from bro.llm import wires
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest


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
