from types import SimpleNamespace
from typing import Any, Optional

import openai
import pytest
from pydantic import BaseModel

import bro.llm.mu as mu_module
from bro.llm.mu import Text, mu


class _Answer(BaseModel):
  answer: str


class _FakeClient:
  def __init__(self, responses: Any):
    self.responses = responses

  async def __aenter__(self) -> '_FakeClient':
    return self

  async def __aexit__(self, *exception_info: Any) -> bool:
    return False


def _install_fake_client(monkeypatch, parsed: Optional[_Answer]) -> list[dict]:
  calls: list[dict] = []

  async def parse(**arguments: Any) -> Any:
    calls.append(arguments)
    return SimpleNamespace(output_parsed=parsed)

  monkeypatch.setattr(mu_module.credentials, 'get_json', lambda name: {'api_key': 'key'})
  monkeypatch.setattr(
    openai, 'AsyncOpenAI', lambda api_key: _FakeClient(SimpleNamespace(parse=parse))
  )
  return calls


def test_call_returns_parsed_result_without_a_caller_loop(monkeypatch):
  calls = _install_fake_client(monkeypatch, _Answer(answer='42'))

  assert mu('what is it?', _Answer, Text('the record')).answer == '42'
  assert calls[0]['text_format'] is _Answer


def test_call_defaults_to_the_shared_model(monkeypatch):
  calls = _install_fake_client(monkeypatch, _Answer(answer='42'))

  mu('what is it?', _Answer)

  assert calls[0]['model'] == mu_module.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_explicit_model_overrides_the_default(monkeypatch):
  calls = _install_fake_client(monkeypatch, _Answer(answer='42'))

  await mu.aio('what is it?', _Answer, model='gpt-5.6-luna', reasoning_effort='low')

  assert calls[0]['model'] == 'gpt-5.6-luna'
  assert calls[0]['reasoning'] == {'effort': 'low'}


@pytest.mark.asyncio
async def test_aio_returns_parsed_result(monkeypatch):
  _install_fake_client(monkeypatch, _Answer(answer='42'))

  assert (await mu.aio('what is it?', _Answer)).answer == '42'


def test_unparsed_response_raises(monkeypatch):
  _install_fake_client(monkeypatch, None)

  with pytest.raises(RuntimeError, match='no parsed output'):
    mu('what is it?', _Answer)
