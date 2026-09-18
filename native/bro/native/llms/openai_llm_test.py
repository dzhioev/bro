"""Live Responses API probe for notification delivery.

A tool batch may be followed, under ``previous_response_id``, by both its
``function_call_output`` and a user-role notification item.
"""

import pytest
from openai import AsyncOpenAI
from openai.types.responses.function_tool_param import FunctionToolParam

from bro.base import credentials
from bro.base.suite_environment import host_credential_store
from bro.llm.llms.openai import DEFAULT_MODEL


def _host_holds_the_openai_key() -> bool:
  with host_credential_store():
    return credentials.available('openai')


pytestmark = pytest.mark.skipif(
  not _host_holds_the_openai_key(), reason='needs the openai credential'
)


@pytest.mark.asyncio
async def test_user_notification_after_function_output_is_accepted():
  with host_credential_store():
    client = AsyncOpenAI(api_key=credentials.get_json('openai')['api_key'])

  tool = FunctionToolParam(
    type='function',
    name='probe',
    description='return a fixed probe value',
    parameters={'type': 'object', 'properties': {}},
    strict=False,
  )
  first = await client.responses.create(
    model=DEFAULT_MODEL,
    input='Call the probe tool.',
    tools=[tool],
    tool_choice={'type': 'function', 'name': 'probe'},
    reasoning={'effort': 'low'},
  )
  calls = [item for item in first.output if item.type == 'function_call']
  assert len(calls) == 1, first.output

  second = await client.responses.create(
    model=DEFAULT_MODEL,
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
