from decimal import Decimal
from types import MappingProxyType

import httpx2
import openai
import pytest

from bro.deepseek.declaration import PRICE_TABLE, Recipe, price, price_table_from_content
from bro.llm.llm import Recipe as BaseRecipe


@pytest.mark.parametrize(
  'effort,expected',
  [('low', 'low'), ('high', 'high'), ('max', 'max')],
)
def test_native_effort_drives_thinking_request(effort, expected):
  assert Recipe(model='deepseek-v4-pro', reasoning_effort=effort).request_options() == {
    'thinking': {'type': 'enabled'},
    'reasoning_effort': expected,
  }


def test_deferred_and_disabled_thinking_and_content_capabilities():
  assert Recipe(model='deepseek-v4-pro').request_options() == {}
  assert Recipe(model='deepseek-v4-pro', reasoning_effort='none').request_options() == {
    'thinking': {'type': 'disabled'}
  }
  assert Recipe(model='deepseek-v4-pro').needed_secrets() == ('deepseek',)
  assert not Recipe(model='deepseek-v4-pro').accepts('image/png')
  assert Recipe(model='deepseek-flash').accepts('image/png')
  assert not Recipe(model='deepseek-flash').accepts('application/pdf')
  with pytest.raises(ValueError, match='reasoning_effort'):
    BaseRecipe.from_dict(
      {'type': 'deepseek', 'model': 'deepseek-v4-pro', 'reasoning_effort': 'medium'}
    )
  assert Recipe(model='opaque-model', passthrough=True).admitted()


@pytest.mark.parametrize(
  'status,expected',
  [(400, False), (401, False), (402, False), (422, False), (429, True), (500, True), (503, True)],
)
def test_retry_classifies_http_errors(status, expected):
  response = httpx2.Response(status, request=httpx2.Request('POST', 'https://fixture'))
  error = openai.APIStatusError('fixture error', response=response, body=None)
  assert Recipe(model='deepseek-v4-pro').is_transient(error) is expected


def test_connection_and_timeout_errors_are_transient_and_application_errors_are_not():
  request = httpx2.Request('POST', 'https://fixture')
  assert Recipe(model='deepseek-v4-pro').is_transient(openai.APIConnectionError(request=request))
  assert Recipe(model='deepseek-v4-pro').is_transient(openai.APITimeoutError(request=request))
  assert not Recipe(model='deepseek-v4-pro').is_transient(ValueError('bad recipe'))


def test_price_uses_cache_partition_and_output_including_reasoning():
  table = price_table_from_content(
    'https://fixture', '2026-10-29', {'model': {'cache_hit': '1', 'cache_miss': '2', 'output': '3'}}
  )
  usage = {
    'prompt_tokens': 100,
    'prompt_cache_hit_tokens': 80,
    'prompt_cache_miss_tokens': 20,
    'completion_tokens': 10,
    'completion_tokens_details': {'reasoning_tokens': 7},
  }
  assert price('model', usage, None, table) == Decimal('0.00015')
  assert price('model-snapshot', usage, None, table) is None
  assert price('deepseek-unknown', usage, None) is None
  for name, value in [
    ('prompt_tokens', 101),
    ('prompt_cache_hit_tokens', -1),
    ('completion_tokens', True),
  ]:
    with pytest.raises(ValueError):
      price('model', {**usage, name: value}, None, table)


def test_price_table_round_trip_and_provenance_are_immutable():
  table = price_table_from_content(
    PRICE_TABLE.source,
    PRICE_TABLE.as_of.isoformat(),
    {model: rates.content() for model, rates in PRICE_TABLE.models.items()},
  )
  assert table == PRICE_TABLE
  assert table.sha256 == PRICE_TABLE.sha256
  assert isinstance(table.models, MappingProxyType)
  assert Recipe.MODEL_INPUTS.keys() <= table.models.keys()
  assert table.source.startswith('https://')


@pytest.mark.parametrize(
  'rates',
  [
    {'cache_hit': 'NaN', 'cache_miss': '1', 'output': '1'},
    {'cache_hit': '-1', 'cache_miss': '1', 'output': '1'},
    {'cache_hit': 1, 'cache_miss': '1', 'output': '1'},
    {'output': '1'},
  ],
)
def test_malformed_persisted_rates_raise(rates):
  with pytest.raises(ValueError):
    price_table_from_content('https://fixture', '2026-10-29', {'model': rates})
