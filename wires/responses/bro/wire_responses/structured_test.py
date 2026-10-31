from dataclasses import replace
from types import SimpleNamespace

import httpx2
import openai
import pytest
from pydantic import BaseModel, ValidationError

from bro.llm.llm import LLMStopped, UnsupportedContent
from bro.llm.wire import File, Prefix, Text
from bro.openai.declaration import LLMSpec
from bro.wire_responses import client as client_module
from bro.wire_responses.client import Responses


class _Answer(BaseModel):
  answer: str


def _response(text='{"answer":"42"}', **fields):
  raw = {
    'id': 'response',
    'model': 'model',
    'status': 'completed',
    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
    **fields,
  }
  return SimpleNamespace(model_dump=lambda **kwargs: raw)


@pytest.fixture
def client(monkeypatch):
  class _Client:
    def __init__(self):
      self.responses = self
      self.requests = []
      self.replies = [_response()]
      self.closed = False

    async def create(self, **request):
      self.requests.append(request)
      reply = self.replies.pop(0)
      if isinstance(reply, Exception):
        raise reply
      return reply

    async def __aenter__(self):
      return self

    async def __aexit__(self, *exception_info):
      self.closed = True

  client = _Client()
  monkeypatch.setattr(client_module, 'AsyncOpenAI', lambda **kwargs: client)
  return client


@pytest.mark.asyncio
async def test_structured_enforces_the_schema_and_options_without_conversation_state(client):
  recipe = LLMSpec(reasoning_effort='high', service_tier='priority')
  wire = Responses(recipe, {'api_key': 'key'})
  wire.restore(Prefix('restored prompt', ()))
  wire.seed_chain('previous')
  result = await wire.structured('question', _Answer, (Text('record'),), 17)
  assert result.answer == '42'
  request = client.requests[0]
  assert request['input'] == [
    {'role': 'system', 'content': 'question'},
    {'role': 'user', 'content': 'record'},
  ]
  assert 'previous_response_id' not in request
  assert request['model'] == recipe.model
  assert request['timeout'] == 17
  for name, value in recipe.request_options().items():
    assert request[name] == value
  output_format = request['text']['format']
  assert output_format['type'] == 'json_schema'
  assert output_format['strict'] is True
  assert 'answer' in output_format['schema']['properties']
  assert client.closed
  assert wire._last_response_id == 'previous'


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ('status', 'reason', 'stop'),
  [
    ('incomplete', 'max_output_tokens', 'truncated'),
    ('incomplete', 'content_filter', 'filtered'),
    ('incomplete', 'other', 'failed'),
    ('failed', None, 'failed'),
  ],
)
async def test_stops_are_checked_before_partial_json_is_validated(client, status, reason, stop):
  client.replies = [_response('{"answer":', status=status, incomplete_details={'reason': reason})]
  with pytest.raises(LLMStopped) as error:
    await Responses(LLMSpec(), {'api_key': 'key'}).structured(
      'question', _Answer, (Text('input'),), None
    )
  assert error.value.reason == stop
  assert error.value.stop_reason == (reason or status)
  assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['', 'not JSON', '{"wrong":"field"}'])
async def test_empty_or_invalid_completed_replies_raise(client, text):
  client.replies = [_response(text)]
  with pytest.raises((RuntimeError, ValidationError)):
    await Responses(LLMSpec(), {'api_key': 'key'}).structured(
      'question', _Answer, (Text('input'),), None
    )
  assert client.closed


@pytest.mark.asyncio
async def test_a_refusal_is_not_validated_as_an_answer(client):
  client.replies = [
    _response(output=[{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'no'}]}])
  ]
  with pytest.raises(RuntimeError, match='refusal'):
    await Responses(LLMSpec(), {'api_key': 'key'}).structured(
      'question', _Answer, (Text('input'),), None
    )


@pytest.mark.asyncio
async def test_unsupported_content_is_refused_before_a_request(client):
  with pytest.raises(UnsupportedContent, match='audio.*gpt'):
    await Responses(LLMSpec(), {'api_key': 'key'}).structured(
      'question', _Answer, (File(b'audio', 'audio/wav'),), None
    )
  assert client.requests == []


@pytest.mark.asyncio
async def test_transient_errors_use_the_recipe_retry_and_close_the_client(client, monkeypatch):
  waits = []

  async def wait(delay):
    waits.append(delay)

  monkeypatch.setattr('bro.base.retry.asyncio_sleep', wait)
  client.replies = [
    openai.APIConnectionError(request=httpx2.Request('POST', 'https://test')),
    _response(),
  ]
  answer = await Responses(LLMSpec(), {'api_key': 'key'}).structured(
    'question', _Answer, (Text('input'),), None
  )
  assert answer.answer == '42'
  assert len(client.requests) == 2
  assert waits
  assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('exhausted', [False, True])
async def test_capacity_uses_the_shared_retry_without_chaining(client, monkeypatch, exhausted):
  from bro.llm import retry

  decode = client_module.decode

  def with_capacity(raw):
    decoded = decode(raw)
    return (
      replace(decoded, stop='capacity', stop_reason='capacity') if raw.get('capacity') else decoded
    )

  waits = []

  async def wait(delay):
    waits.append(delay)

  monkeypatch.setattr(client_module, 'decode', with_capacity)
  monkeypatch.setattr(retry, 'asyncio_sleep', wait)
  capacity = _response(capacity=True)
  client.replies = [capacity] * 7 if exhausted else [capacity, _response()]
  wire = Responses(LLMSpec(), {'api_key': 'key'})
  if exhausted:
    with pytest.raises(LLMStopped, match='capacity'):
      await wire.structured('question', _Answer, (Text('input'),), None)
    assert len(client.requests) == 7
    assert len(waits) == 6
  else:
    assert (await wire.structured('question', _Answer, (Text('input'),), None)).answer == '42'
    assert len(waits) == 1
  assert all(request == client.requests[0] for request in client.requests)
  assert wire._last_response_id is None
  assert client.closed
