from types import SimpleNamespace
from typing import Any, Optional

import openai
import pytest
from pydantic import BaseModel

import bro.llm.mu as mu_module
from bro.llm.mu import IncompleteResponse, Text, TruncatedResponse, mu


class _Answer(BaseModel):
  answer: str


class _FakeClient:
  def __init__(self, responses: Any):
    self.responses = responses

  async def __aenter__(self) -> '_FakeClient':
    return self

  async def __aexit__(self, *exception_info: Any) -> bool:
    return False


def _response(
  output_text: str, status: str = 'completed', incomplete_reason: Optional[str] = None
) -> Any:
  details = SimpleNamespace(reason=incomplete_reason) if incomplete_reason is not None else None
  return SimpleNamespace(
    output_text=output_text,
    status=status,
    incomplete_details=details,
    usage=SimpleNamespace(input_tokens=390_000, output_tokens=2_000),
  )


def _install_fake_client(monkeypatch, response: Any) -> list[dict]:
  calls: list[dict] = []

  async def create(**arguments: Any) -> Any:
    calls.append(arguments)
    return response

  monkeypatch.setattr(mu_module.credentials, 'get_json', lambda name: {'api_key': 'key'})
  monkeypatch.setattr(
    openai, 'AsyncOpenAI', lambda api_key: _FakeClient(SimpleNamespace(create=create))
  )
  return calls


def test_call_returns_parsed_result_without_a_caller_loop(monkeypatch):
  calls = _install_fake_client(monkeypatch, _response('{"answer": "42"}'))

  assert mu('what is it?', _Answer, Text('the record')).answer == '42'
  text_format = calls[0]['text']['format']
  assert text_format['type'] == 'json_schema'
  assert text_format['strict'] is True
  assert 'answer' in text_format['schema']['properties']


def test_call_defaults_to_the_shared_model(monkeypatch):
  calls = _install_fake_client(monkeypatch, _response('{"answer": "42"}'))

  mu('what is it?', _Answer)

  assert calls[0]['model'] == mu_module.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_explicit_model_overrides_the_default(monkeypatch):
  calls = _install_fake_client(monkeypatch, _response('{"answer": "42"}'))

  await mu.aio('what is it?', _Answer, model='gpt-5.6-luna', reasoning_effort='low')

  assert calls[0]['model'] == 'gpt-5.6-luna'
  assert calls[0]['reasoning'] == {'effort': 'low'}


@pytest.mark.asyncio
async def test_aio_returns_parsed_result(monkeypatch):
  _install_fake_client(monkeypatch, _response('{"answer": "42"}'))

  assert (await mu.aio('what is it?', _Answer)).answer == '42'


def test_response_cut_off_at_the_output_limit_raises_truncated(monkeypatch):
  _install_fake_client(monkeypatch, _response('{"answer": "4', 'incomplete', 'max_output_tokens'))

  with pytest.raises(TruncatedResponse, match='390000 input / 2000 output tokens'):
    mu('what is it?', _Answer)


def test_response_stopped_for_another_reason_raises_incomplete(monkeypatch):
  _install_fake_client(monkeypatch, _response('', 'incomplete', 'content_filter'))

  with pytest.raises(IncompleteResponse, match='content_filter') as error:
    mu('what is it?', _Answer)
  assert not isinstance(error.value, TruncatedResponse)


def test_response_without_output_text_raises(monkeypatch):
  _install_fake_client(monkeypatch, _response(''))

  with pytest.raises(RuntimeError, match='no output text'):
    mu('what is it?', _Answer)
