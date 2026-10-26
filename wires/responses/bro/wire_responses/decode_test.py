import pytest

from bro.llm.records import Call, Message, Reasoning, Unknown
from bro.wire_responses.decode import decode


def test_parts_follow_output_indexes_and_summary_texts_share_their_item_index():
  raw = {
    'model': 'model',
    'status': 'completed',
    'service_tier': 'priority',
    'output': [
      {
        'type': 'reasoning',
        'summary': [
          {'type': 'summary_text', 'text': 'one'},
          {'type': 'summary_text', 'text': ''},
          {'type': 'summary_text', 'text': 'two'},
        ],
      },
      {
        'type': 'message',
        'phase': 'commentary',
        'content': [{'type': 'output_text', 'text': 'working'}],
      },
      {'type': 'function_call', 'call_id': 'c1', 'name': 'read', 'arguments': '{}'},
      {'type': 'compaction', 'encrypted_content': 'opaque'},
    ],
  }
  decoded = decode(raw)
  assert decoded.parts[:3] == (
    Reasoning(1, 'one'),
    Reasoning(1, 'two'),
    Message(2, 'working', commentary=True),
  )
  assert isinstance(decoded.parts[3], Call)
  assert decoded.calls[0].index == 3
  assert decoded.parts[4] == Unknown(4, raw['output'][3])
  assert decoded.service_tier == 'priority'
  assert not decoded.terminal


def test_message_joins_output_text_and_reports_refusal_separately():
  decoded = decode(
    {
      'status': 'completed',
      'output': [
        {
          'type': 'message',
          'content': [
            {'type': 'output_text', 'text': 'a'},
            {'type': 'output_text', 'text': 'b'},
            {'type': 'refusal', 'refusal': 'declined'},
          ],
        }
      ],
    }
  )
  assert decoded.reply == 'ab'
  assert decoded.refusal == 'declined'


@pytest.mark.parametrize(
  ('status', 'reason', 'stop'),
  [
    ('completed', None, 'completed'),
    ('incomplete', 'max_output_tokens', 'truncated'),
    ('incomplete', 'content_filter', 'filtered'),
    ('incomplete', 'other', 'failed'),
    ('failed', None, 'failed'),
    ('other', None, 'failed'),
    (None, None, 'failed'),
  ],
)
def test_status_and_incomplete_reason(status, reason, stop):
  decoded = decode({'status': status, 'incomplete_details': {'reason': reason}, 'output': []})
  assert decoded.stop == stop
  assert decoded.stop_reason == (reason or status)


@pytest.mark.parametrize(
  'output',
  [
    None,
    [None],
    [{'type': 'reasoning', 'summary': None}],
    [{'type': 'message', 'content': None}],
    [{'type': 'future'}],
  ],
)
def test_unknown_shapes_decode_without_raising(output):
  assert isinstance(decode({'output': output}).parts[0], Unknown)
