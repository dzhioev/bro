"""Live Responses API probes for notification delivery and pricing.

A tool batch may be followed, under ``previous_response_id``, by both its
``function_call_output`` and a user-role notification item.
Every listed model, served at each service tier its price row carries,
reports a model id and a tier that price the call at the requested tier.
"""

import contextlib
from collections.abc import AsyncIterator

import pytest
from openai import AsyncOpenAI
from openai.types.responses.function_tool_param import FunctionToolParam

import bro.openai.declaration as openai_llm
from bro.base import credentials
from bro.base.suite_environment import host_credential_store


def _host_holds_the_openai_key() -> bool:
  with host_credential_store():
    return credentials.available('openai')


pytestmark = pytest.mark.skipif(
  not _host_holds_the_openai_key(), reason='needs the openai credential'
)


@contextlib.asynccontextmanager
async def _openai_client() -> AsyncIterator[AsyncOpenAI]:
  with host_credential_store():
    api_key = credentials.get_json('openai')['api_key']
  async with AsyncOpenAI(api_key=api_key) as client:
    yield client


@pytest.mark.asyncio
async def test_user_notification_after_function_output_is_accepted():
  async with _openai_client() as client:
    tool = FunctionToolParam(
      type='function',
      name='probe',
      description='return a fixed probe value',
      parameters={'type': 'object', 'properties': {}},
      strict=False,
    )
    first = await client.responses.create(
      model='gpt-5.6-terra',
      input='Call the probe tool.',
      tools=[tool],
      tool_choice={'type': 'function', 'name': 'probe'},
      reasoning={'effort': 'low'},
    )
    calls = [item for item in first.output if item.type == 'function_call']
    assert len(calls) == 1, first.output

    second = await client.responses.create(
      model='gpt-5.6-terra',
      previous_response_id=first.id,
      input=[
        {
          'type': 'function_call_output',
          'call_id': calls[0].call_id,
          'output': 'probe finished',
        },
        {
          'role': 'user',
          'content': '[notification: a background job reported]\nprobe news',
        },
      ],
      tools=[tool],
      reasoning={'effort': 'low'},
    )

    assert any(item.type == 'message' for item in second.output), second.output


_REQUEST_VALUE_BY_PRICED_TIER = {
  'standard': 'default',
  'flex': 'flex',
  'priority': 'fast',
  'ultrafast': 'ultrafast',
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'model,service_tier',
  [
    (model, _REQUEST_VALUE_BY_PRICED_TIER[tier])
    for model in sorted(openai_llm.Recipe.MODEL_INPUTS)
    for tier in openai_llm.PRICE_TABLE.models[model].service_tiers
  ],
)
async def test_a_listed_model_reports_a_model_and_tier_priced_as_requested(model, service_tier):
  async with _openai_client() as client:
    response = await client.responses.create(
      model=model,
      input='Reply with the single word ok.',
      reasoning={'effort': 'low'},
      service_tier=service_tier,
    )

    raw = response.model_dump(mode='json')
    cost = openai_llm.price(raw['model'], raw['usage'], raw['service_tier'])
    assert cost is not None, (raw['model'], raw['service_tier'])
    assert cost == openai_llm.price(model, raw['usage'], service_tier), (
      raw['model'],
      raw['service_tier'],
    )
