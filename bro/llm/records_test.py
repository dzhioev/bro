import pytest

from bro.llm.records import Call, DecodedCall, Message, Reasoning


@pytest.mark.parametrize(
  ('raw', 'arguments', 'valid'),
  [
    ('{}', {}, True),
    ('', {}, True),
    (' \n\t', {}, True),
    ('{"value": 3}', {'value': 3}, True),
    ('broken', {'_raw_arguments': 'broken'}, False),
    ('[]', [], False),
    ('null', None, False),
    ('3', 3, False),
  ],
)
def test_call_preserves_raw_arguments_and_distinguishes_invalid_objects(raw, arguments, valid):
  call = Call.parse(2, 'call-1', 'read', raw)
  assert call.raw_arguments == raw
  assert call.arguments == arguments
  assert call.arguments_are_object is valid


def test_reply_excludes_commentary_and_calls_make_text_interim():
  parts = (
    Reasoning(1, 'think'),
    Message(2, 'working', commentary=True),
    Message(3, 'done'),
    Message(4, 'more'),
  )
  decoded = DecodedCall('model', {}, None, parts, 'completed')
  assert decoded.reply == 'done\n\nmore'
  assert decoded.terminal is True
  with_call = DecodedCall(
    'model', {}, None, (*parts, Call.parse(5, 'c1', 'read', '{}')), 'completed'
  )
  assert with_call.reply is None
  assert with_call.terminal is False
  assert [call.index for call in with_call.calls] == [5]


@pytest.mark.parametrize('stop', ['truncated', 'filtered', 'capacity', 'failed'])
def test_incomplete_text_is_not_terminal(stop):
  assert not DecodedCall('model', {}, None, (Message(1, 'partial'),), stop).terminal
