import asyncio
import json
from copy import deepcopy

import httpx2
import pytest
from pydantic import BaseModel, ValidationError

from bro.deepseek.declaration import LLMSpec
from bro.llm.llm import LLMStopped, UnsupportedContent
from bro.llm.mcp import FunctionTool, Tool
from bro.llm.wire import Notification, Prefix, SystemPrompt, ToolOutput, UserMessage
from bro.trails.model import Step
from bro.wire_chat.client import Chat
from bro.wire_chat.fixtures_test_helper import call, response


@pytest.fixture
def transport(monkeypatch):
  requests = []
  replies = []

  async def send(client, request, **kwargs):
    requests.append(json.loads(await request.aread()))
    item = replies.pop(0)
    if isinstance(item, BaseException):
      raise item
    return httpx2.Response(200, request=request, json=deepcopy(item))

  monkeypatch.setattr(httpx2.AsyncClient, 'send', send)
  return requests, replies


@pytest.mark.asyncio
async def test_history_preserves_reasoning_null_content_and_raw_arguments(transport):
  requests, replies = transport
  wire = Chat(LLMSpec(reasoning_effort='high'), {'api_key': 'fixture-key'})
  replies.extend(
    [
      response(None, reasoning='first', calls=[call('')], stop='tool_calls'),
      response('answer', reasoning='second'),
      response('next'),
    ]
  )

  def ping() -> str:
    return 'pong'

  tools: list[Tool] = [FunctionTool(ping, description='Ping')]
  first = await wire.exchange([SystemPrompt('system'), UserMessage('go')], tools, 4)
  second = await wire.exchange([ToolOutput('call_1', {'value': 'pong'})], tools, None)
  await wire.exchange([Notification('news'), UserMessage('continue')], tools, None)
  assert requests[1]['messages'][2] == {
    'role': 'assistant',
    'content': None,
    'reasoning_content': 'first',
    'tool_calls': [call('')],
  }
  assert requests[2]['messages'][2] == requests[1]['messages'][2]
  assert requests[2]['messages'][4] == {
    'role': 'assistant',
    'content': 'answer',
    'reasoning_content': 'second',
  }
  assert requests[1]['messages'][3] == {
    'role': 'tool',
    'tool_call_id': 'call_1',
    'content': '{"value":"pong"}',
  }
  assert second.record['request']['messages'] == [requests[1]['messages'][3]]
  assert first.record['response']['choices'][0]['message']['content'] is None
  assert requests[0]['tools'] == [
    {
      'type': 'function',
      'function': {'name': 'ping', 'description': 'Ping', 'parameters': tools[0].parameters},
    }
  ]
  assert requests[0]['thinking'] == {'type': 'enabled'}
  assert requests[0]['reasoning_effort'] == 'high'
  assert (
    not {'strict', 'tool_choice', 'max_tokens', 'user_id', 'temperature', 'top_p'}
    & requests[0].keys()
  )


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'stop', ['length', 'content_filter', 'insufficient_system_resource', 'aborted']
)
async def test_uncompleted_response_does_not_advance_history(transport, stop):
  requests, replies = transport
  replies.extend([response('discard', reasoning='discard', stop=stop), response()])
  wire = Chat(LLMSpec(), {'api_key': 'fixture-key'})
  assert (await wire.exchange([UserMessage('retry')], [], None)).stop != 'completed'
  await wire.exchange([UserMessage('retry')], [], None)
  assert requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'message',
  [
    {'role': 'assistant', 'refusal': 'no'},
    {'role': 'assistant', 'tool_calls': [call(name='')]},
    {'role': 'assistant', 'tool_calls': [call(call_id='')]},
    {'role': 'assistant', 'tool_calls': [{'type': 'custom'}]},
  ],
)
async def test_unusable_completion_raises_without_advancing(transport, message):
  requests, replies = transport
  raw = response()
  raw['choices'][0]['message'] = message
  replies.extend([raw, response()])
  wire = Chat(LLMSpec(), {'api_key': 'fixture-key'})
  with pytest.raises(RuntimeError):
    await wire.exchange([UserMessage('retry')], [], None)
  await wire.exchange([UserMessage('retry')], [], None)
  assert requests[0] == requests[1]


@pytest.mark.asyncio
async def test_cancelled_exchange_keeps_history(transport):
  requests, replies = transport
  replies.extend([asyncio.CancelledError(), response()])
  wire = Chat(LLMSpec(), {'api_key': 'fixture-key'})
  with pytest.raises(asyncio.CancelledError):
    await wire.exchange([UserMessage('retry')], [], None)
  await wire.exchange([UserMessage('retry')], [], None)
  assert requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'silent,replayed',
  [
    (response(None), {'role': 'assistant', 'content': ''}),
    (
      response('', reasoning='waiting'),
      {'role': 'assistant', 'content': '', 'reasoning_content': 'waiting'},
    ),
  ],
  ids=['empty', 'reasoning-only'],
)
async def test_silent_completion_ends_the_turn_and_replays_as_sent_live(
  transport, silent, replayed
):
  requests, replies = transport
  replies.extend([silent, response(), response()])
  live = Chat(LLMSpec(), {'api_key': 'fixture-key'})
  leg = await live.exchange([SystemPrompt('system'), UserMessage('go')], [], None)
  assert leg.reply == ''
  await live.exchange([Notification('news')], [], None)
  restored = Chat(LLMSpec(), {'api_key': 'fixture-key'})
  restored.restore(Prefix('system', (step('user_input', 'go'), step('llm_call', leg.record))))
  await restored.exchange([Notification('news')], [], None)
  assert (
    requests[1]['messages']
    == requests[2]['messages']
    == [
      {'role': 'system', 'content': 'system'},
      {'role': 'user', 'content': 'go'},
      replayed,
      {'role': 'user', 'content': 'news'},
    ]
  )


def step(kind, body, **extras):
  return Step(
    trail_id='test', step_id=1, ts='2026-01-01T00:00:00Z', kind=kind, body=body, extras=extras
  )


@pytest.mark.asyncio
async def test_restore_uses_steps_not_recorded_requests_and_omits_stopped_legs(transport):
  requests, replies = transport
  raw = response(None, reasoning='kept', calls=[call('')], stop='tool_calls')
  raw['choices'][0]['message'].update(refusal=None, annotations=[], audio=None)
  wire = Chat(LLMSpec(), {'api_key': 'fixture-key'})
  wire.restore(
    Prefix(
      'system',
      (
        step('user_input', 'original'),
        step('llm_call', {'request': {'messages': ['poison']}, 'response': raw}),
        step('tool_result', {'answer': 42}, call_id='call_1'),
        step('notification', 'news'),
        step('llm_call', {'response': response('discard')}, stopped='truncated'),
      ),
    )
  )
  replies.append(response())
  await wire.exchange([UserMessage('continue')], [], None)
  assert requests[0]['messages'] == [
    {'role': 'system', 'content': 'system'},
    {'role': 'user', 'content': 'original'},
    {'role': 'assistant', 'content': None, 'reasoning_content': 'kept', 'tool_calls': [call('')]},
    {'role': 'tool', 'tool_call_id': 'call_1', 'content': '{"answer":42}'},
    {'role': 'user', 'content': 'news'},
    {'role': 'user', 'content': 'continue'},
  ]


class Answer(BaseModel):
  answer: int


@pytest.mark.asyncio
@pytest.mark.parametrize('exhausted', [False, True])
async def test_structured_json_schema_validation_capacity_and_no_history(
  transport, monkeypatch, exhausted
):
  requests, replies = transport
  waits = []

  async def wait(seconds):
    waits.append(seconds)

  monkeypatch.setattr('bro.llm.retry.asyncio_sleep', wait)
  capacity = response(stop='insufficient_system_resource')
  replies.extend([capacity] * 7 if exhausted else [capacity, response('{"answer":42}')])
  wire = Chat(LLMSpec(reasoning_effort='none'), {'api_key': 'fixture-key'})
  wire.restore(Prefix('not part of structured', ()))
  if exhausted:
    with pytest.raises(LLMStopped, match='capacity'):
      await wire.structured('question', Answer, [{'type': 'text', 'text': 'input'}], 3)
    assert len(requests) == 7
    assert sum(waits) == 610
  else:
    assert (await wire.structured('question', Answer, [], 3)).answer == 42
    assert waits == [10]
  assert all(request == requests[0] for request in requests)
  assert requests[0]['response_format'] == {'type': 'json_object'}
  prompt = requests[0]['messages'][0]['content']
  assert json.loads(prompt.split('schema:\n')[1]) == Answer.model_json_schema()
  assert 'not part of structured' not in prompt
  assert requests[0]['thinking'] == {'type': 'disabled'}
  assert wire.client.is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'text,stop,error',
  [
    ('not JSON', 'stop', ValidationError),
    ('{"answer":"wrong"}', 'stop', ValidationError),
    ('partial', 'length', LLMStopped),
    (None, 'stop', RuntimeError),
  ],
)
async def test_structured_rejects_invalid_or_uncompleted_reply(transport, text, stop, error):
  _, replies = transport
  replies.append(response(text, stop=stop))
  with pytest.raises(error):
    await Chat(LLMSpec(), {'api_key': 'fixture-key'}).structured('question', Answer, [], None)


@pytest.mark.asyncio
async def test_vision_is_gated_before_file_access_and_encodes_permitted_image(transport, tmp_path):
  requests, replies = transport
  path = tmp_path / 'image.png'
  content = [{'type': 'image_url', 'image_url': {'url': str(path)}}]
  with pytest.raises(UnsupportedContent, match='image.*not supported'):
    await Chat(LLMSpec(), {'api_key': 'fixture-key'}).exchange([UserMessage(content)], [], None)
  assert not requests
  path.write_bytes(b'image fixture')
  replies.append(response())
  await Chat(LLMSpec(model='deepseek-flash'), {'api_key': 'fixture-key'}).exchange(
    [UserMessage(content)], [], None
  )
  assert requests[0]['messages'][0]['content'] == [
    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aW1hZ2UgZml4dHVyZQ=='}}
  ]
