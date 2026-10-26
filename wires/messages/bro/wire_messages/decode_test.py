import pytest

from bro.llm.records import Message, Reasoning, Unknown
from bro.wire_messages.decode import decode


def test_content_block_indexes_and_structured_tool_input():
  content = [
    {'type': 'thinking', 'thinking': 'think'},
    {'type': 'text', 'text': 'checking'},
    {'type': 'tool_use', 'id': 'c1', 'name': 'read', 'input': {'path': 'file'}},
    {'type': 'redacted_thinking', 'data': 'opaque'},
  ]
  decoded = decode({'content': content, 'stop_reason': 'tool_use'})
  assert decoded.parts[:2] == (Reasoning(1, 'think'), Message(2, 'checking'))
  assert decoded.calls[0].index == 3
  assert decoded.calls[0].arguments == {'path': 'file'}
  assert decoded.calls[0].arguments_are_object
  assert decoded.parts[3] == Unknown(4, content[3])
  assert not decoded.terminal


@pytest.mark.parametrize(
  ('reason', 'stop'),
  [
    ('max_tokens', 'truncated'),
    ('refusal', 'filtered'),
    ('end_turn', 'completed'),
    ('tool_use', 'completed'),
    ('stop_sequence', 'completed'),
    (None, 'completed'),
    ('other', 'completed'),
  ],
)
def test_stop_reasons_include_partial_transcript_messages(reason, stop):
  decoded = decode({'content': [{'type': 'text', 'text': 'reply'}], 'stop_reason': reason})
  assert decoded.stop == stop
  assert decoded.stop_reason == reason
  assert decoded.terminal is (stop == 'completed')


@pytest.mark.parametrize('content', [None, [None], [{'type': 'future'}]])
def test_unknown_content_survives(content):
  assert isinstance(decode({'content': content}).parts[0], Unknown)
