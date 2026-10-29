import pytest

from bro.llm.records import Call, Message, Reasoning, Unknown
from bro.wire_chat.decode import counts, decode


# Fixtures follow https://api-docs.deepseek.com/api/create-chat-completion.
def response(
  *,
  reasoning: str | None = 'thinking',
  content: str | None = 'reply',
  calls=None,
  finish='stop',
  usage=None,
):
  return {
    'id': 'chat-1',
    'model': 'deepseek-v4-pro',
    'choices': [
      {
        'index': 0,
        'message': {
          'role': 'assistant',
          'reasoning_content': reasoning,
          'content': content,
          'tool_calls': calls,
        },
        'finish_reason': finish,
      }
    ],
    'usage': usage,
  }


def tool_call(arguments='{}'):
  return {'id': 'call-1', 'type': 'function', 'function': {'name': 'read', 'arguments': arguments}}


def test_reasoning_and_reply_keep_their_sequence_and_raw_usage():
  raw_usage = {
    'prompt_tokens': 1000,
    'prompt_cache_hit_tokens': 900,
    'prompt_cache_miss_tokens': 100,
    'completion_tokens': 50,
    'completion_tokens_details': {'reasoning_tokens': 30},
  }
  decoded = decode(response(usage=raw_usage))
  assert decoded.model == 'deepseek-v4-pro'
  assert decoded.parts == (Reasoning(1, 'thinking'), Message(2, 'reply'))
  assert decoded.reply == 'reply'
  assert decoded.terminal
  assert decoded.usage == raw_usage
  assert counts(decoded.usage) == {'input': 100, 'cache_write': 0, 'cache_read': 900, 'output': 50}


def test_interim_text_precedes_calls():
  decoded = decode(
    response(content='checking', calls=[tool_call(), tool_call('{"x": 1}')], finish='tool_calls')
  )
  assert decoded.parts[:2] == (Reasoning(1, 'thinking'), Message(2, 'checking'))
  assert [call.index for call in decoded.calls] == [3, 4]
  assert decoded.calls[1].arguments == {'x': 1}
  assert decoded.reply is None
  assert not decoded.terminal


@pytest.mark.parametrize('reasoning', [None, ''])
def test_absent_reasoning_and_text_do_not_take_positions(reasoning):
  decoded = decode(
    response(reasoning=reasoning, content=None, calls=[tool_call()], finish='tool_calls')
  )
  assert len(decoded.parts) == 1
  assert isinstance(decoded.parts[0], Call)
  assert decoded.calls[0].index == 1


@pytest.mark.parametrize(
  ('raw', 'arguments', 'valid'),
  [('', {}, True), ('  ', {}, True), ('bad', {'_raw_arguments': 'bad'}, False), ('[]', [], False)],
)
def test_empty_and_malformed_arguments(raw, arguments, valid):
  decoded = decode(response(calls=[tool_call(raw)], finish='tool_calls'))
  assert decoded.calls[0].raw_arguments == raw
  assert decoded.calls[0].arguments == arguments
  assert decoded.calls[0].arguments_are_object is valid


@pytest.mark.parametrize(
  ('finish', 'stop'),
  [
    ('stop', 'completed'),
    ('tool_calls', 'completed'),
    ('length', 'truncated'),
    ('content_filter', 'filtered'),
    ('insufficient_system_resource', 'capacity'),
    ('aborted', 'failed'),
    ('other', 'failed'),
    (None, 'failed'),
  ],
)
def test_every_finish_reason(finish, stop):
  decoded = decode(response(finish=finish))
  assert decoded.stop == stop
  assert decoded.stop_reason == finish
  assert decoded.terminal is (stop == 'completed')


@pytest.mark.parametrize('reasoning', [None, ''])
@pytest.mark.parametrize('content', [None, ''])
def test_a_message_without_reasoning_text_or_calls_is_an_empty_terminal_reply(reasoning, content):
  decoded = decode(response(reasoning=reasoning, content=content))
  assert decoded.parts == ()
  assert decoded.reply == ''
  assert decoded.terminal


@pytest.mark.parametrize('choices', [None, [], [{}, {}], ['invalid']])
def test_choice_count_must_be_exactly_one(choices):
  raw = {**response(), 'choices': choices}
  assert decode(raw).parts == (Unknown(1, raw),)


@pytest.mark.parametrize(
  'message',
  [
    None,
    {'content': [{'type': 'text', 'text': 'unread'}]},
    {'tool_calls': 'invalid'},
    {'tool_calls': [None, {'type': 'future'}]},
  ],
)
def test_unrecognized_shapes_are_preserved(message):
  raw = response()
  raw['choices'][0]['message'] = message
  assert all(isinstance(part, Unknown) for part in decode(raw).parts)


def test_standard_chat_cache_details_and_explicit_hit_count():
  raw = {'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 6}, 'completion_tokens': 4}
  assert counts(raw) == {'input': 4, 'cache_write': 0, 'cache_read': 6, 'output': 4}
  assert counts({**raw, 'prompt_cache_hit_tokens': 0})['input'] == 10


@pytest.mark.parametrize('field', ['id', 'name'])
@pytest.mark.parametrize('value', [None, '', 17])
def test_malformed_tool_identity_is_an_unknown_record_not_a_projectable_call(field, value):
  call = tool_call()
  target = call if field == 'id' else call['function']
  target[field] = value
  decoded = decode(response(reasoning=None, content=None, calls=[call], finish='tool_calls'))
  assert decoded.parts == (Unknown(1, call),)
  assert not decoded.calls
