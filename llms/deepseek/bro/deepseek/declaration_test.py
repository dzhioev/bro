from decimal import Decimal
from types import MappingProxyType

import httpx2
import openai
import pytest

from bro.deepseek.declaration import MODELS, PRICE_TABLE, LLMSpec, price, price_table_from_content
from bro.llm import providers
from bro.llm.llm import LLMSpec as Recipe


@pytest.mark.parametrize(
  'value', ['deepseek:pro:high', 'deepseek:flash:low', 'deepseek:deepseek-v4-flash:max']
)
def test_recipe_resolution_round_trip_and_fast_fallback(value):
  recipe = providers.resolve(None, providers.parse(value), ('deepseek',))
  assert isinstance(recipe, LLMSpec)
  assert Recipe.from_dict(recipe.dump()) == recipe
  assert providers.resolve(None, providers.parse(value + '+fast'), ('deepseek',)) == recipe


@pytest.mark.parametrize(
  'effort,expected',
  [('low', 'low'), ('medium', 'high'), ('high', 'high'), ('xhigh', 'high'), ('max', 'max')],
)
def test_neutral_effort_drives_thinking_request(effort, expected):
  assert LLMSpec().with_effort(effort).request_options() == {
    'thinking': {'type': 'enabled'},
    'reasoning_effort': expected,
  }


def test_deferred_and_disabled_thinking_and_content_capabilities():
  assert LLMSpec().request_options() == {}
  assert LLMSpec(reasoning_effort='none').request_options() == {'thinking': {'type': 'disabled'}}
  assert LLMSpec().needed_secrets() == ('deepseek',)
  assert LLMSpec().accepts('text')
  assert not LLMSpec().accepts('image')
  assert LLMSpec(model=MODELS['flash']).accepts('image')
  assert not LLMSpec(model=MODELS['flash']).accepts('audio')
  with pytest.raises(ValueError, match='reasoning_effort'):
    LLMSpec(reasoning_effort='medium')
  with pytest.raises(ValueError, match='model'):
    LLMSpec(model='invalid')
  with pytest.raises(ValueError, match='effort'):
    LLMSpec().with_effort('none')


@pytest.mark.parametrize(
  'status,expected',
  [(400, False), (401, False), (402, False), (422, False), (429, True), (500, True), (503, True)],
)
def test_retry_classifies_http_errors(status, expected):
  response = httpx2.Response(status, request=httpx2.Request('POST', 'https://fixture'))
  error = openai.APIStatusError('fixture error', response=response, body=None)
  assert LLMSpec().is_transient(error) is expected


def test_connection_and_timeout_errors_are_transient_and_application_errors_are_not():
  request = httpx2.Request('POST', 'https://fixture')
  assert LLMSpec().is_transient(openai.APIConnectionError(request=request))
  assert LLMSpec().is_transient(openai.APITimeoutError(request=request))
  assert not LLMSpec().is_transient(ValueError('bad recipe'))


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
  assert set(MODELS.values()) <= table.models.keys()
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
