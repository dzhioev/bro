import json

from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest


def test_claude_content_uses_messages_decode_without_changing_billing(tmp_path):
  def record(content, stop=None):
    return json.dumps(
      {
        'type': 'assistant',
        'message': {
          'id': 'm1',
          'model': 'claude-model',
          'content': content,
          'stop_reason': stop,
          'usage': {'input_tokens': 7, 'output_tokens': 2},
        },
      }
    )

  records = [
    record(
      [
        {'type': 'thinking', 'thinking': 'think'},
        {'type': 'tool_use', 'id': 'c1', 'name': 'read', 'input': {}},
      ]
    ),
    record([{'type': 'text', 'text': 'reply'}], 'end_turn'),
  ]
  request = BlazeRequest(
    harness='claude',
    version='test',
    interactive=False,
    surface='test',
    native={
      'llm': {},
      'segment': 'segment',
      'ride_command': 'ride solo',
      'harness_version': 'test',
    },
    body={'records': records},
  )
  with LocalStore(tmp_path) as store:
    trail = store.blaze(request)
    messages = list(store.iter_messages(trail['id']))
  assert [event['type'] for event in messages] == [
    'llm_call',
    'reasoning',
    'tool_call',
    'assistant',
  ]
  assert messages[2]['arguments'] == {}
  assert messages[2]['source'] == {'step_id': 0, 'index': 2}
  assert messages[3]['content'] == 'reply'
  assert messages[3]['terminal'] is True
