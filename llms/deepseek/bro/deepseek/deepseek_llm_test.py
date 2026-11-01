"""Live protocol probes; raw replies are saved under pytest's temporary directory.

Run explicitly after installing the DeepSeek credential, never as part of the offline gate.
"""

import json
from copy import deepcopy

import openai
import pytest

from bro.base import credentials
from bro.base.suite_environment import host_credential_store
from bro.deepseek.declaration import Recipe
from bro.deepseek.probe_test_helper import managed_roster
from bro.llm.records import Unknown
from bro.wire_chat.client import assistant_message
from bro.wire_chat.decode import decode


@pytest.mark.asyncio
async def test_deepseek_protocol_shapes_and_native_tool_roster(tmp_path, request, monkeypatch):
  with host_credential_store():
    if not credentials.available('deepseek'):
      pytest.skip('needs the deepseek credential')
    recipe = Recipe(reasoning_effort='low')
    roster = await managed_roster(monkeypatch, recipe)
    client = openai.AsyncOpenAI(
      api_key=credentials.get_json('deepseek')['api_key'], base_url=recipe.ENDPOINT, max_retries=0
    )

  captures = {}

  async def send(name, messages, *, tools=None, options=None, **fields):
    try:
      reply = await client.chat.completions.create(
        model=recipe.model,
        messages=messages,
        extra_body=recipe.request_options() if options is None else options,
        **({'tools': tools} if tools is not None else {}),
        **fields,
      )
    except openai.APIStatusError as error:
      captures[name] = {'status': error.status_code, 'body': error.response.text}
      raise
    else:
      raw = reply.model_dump(mode='json')
      captures[name] = raw
      return raw
    finally:
      (tmp_path / 'deepseek-responses.json').write_text(
        json.dumps(captures, ensure_ascii=False, indent=2)
      )

  tools = [
    {
      'type': 'function',
      'function': {
        'name': name,
        'description': f'Return the {name} probe value.',
        'parameters': {'type': 'object', 'properties': {}},
      },
    }
    for name in ('first_probe', 'second_probe')
  ]
  messages = [
    {'role': 'system', 'content': 'Follow the tool protocol exactly.'},
    {
      'role': 'user',
      'content': 'Call first_probe with no arguments. Do not call second_probe yet.',
    },
  ]

  def probe_calls(raw, name):
    message = assistant_message(raw)
    assert isinstance(message.get('reasoning_content'), str), raw
    assert raw['choices'][0]['finish_reason'] == 'tool_calls', raw
    calls = message.get('tool_calls', [])
    assert calls and all(call['function']['name'] == name for call in calls), raw
    assert all(
      not call['function']['arguments'].strip() or json.loads(call['function']['arguments']) == {}
      for call in calls
    ), raw
    return message, calls

  async with client:
    first = await send('first-leg', messages, tools=tools)
    first_message, calls = probe_calls(first, 'first_probe')
    messages.append(first_message)
    messages.extend(
      {'role': 'tool', 'tool_call_id': call['id'], 'content': 'first value'} for call in calls
    )
    messages.append(
      {
        'role': 'user',
        'content': 'Now call second_probe with no arguments, then wait for its result.',
      }
    )
    second_messages = deepcopy(messages)
    second = await send('second-leg', messages, tools=tools)
    second_message, calls = probe_calls(second, 'second_probe')
    messages.append(second_message)
    messages.extend(
      {'role': 'tool', 'tool_call_id': call['id'], 'content': 'second value'} for call in calls
    )
    messages.append(
      {
        'role': 'user',
        'content': 'Both probes finished. Reply with a short confirmation, no tools.',
      }
    )
    third = await send('third-leg', messages, tools=tools)
    assert third['choices'][0]['finish_reason'] == 'stop', third
    decoded = decode(third)
    assert not any(isinstance(part, Unknown) for part in decoded.parts), third
    assert decoded.reply or decoded.refusal is None, third
    silent = {**assistant_message(third), 'content': ''}
    after_silence = await send(
      'after-empty-turn',
      [*messages, silent, {'role': 'user', 'content': 'Reply with one short word, no tools.'}],
      tools=tools,
    )
    assert after_silence['choices'][0]['finish_reason'] == 'stop', after_silence
    for raw in (first, second, third):
      assert isinstance(raw['model'], str) and raw['model']
      usage = raw['usage']
      assert (
        usage['prompt_tokens']
        == usage['prompt_cache_hit_tokens'] + usage['prompt_cache_miss_tokens']
      )
      assert isinstance(usage['completion_tokens'], int)
      assert isinstance(usage['completion_tokens_details']['reasoning_tokens'], int)

    roster_reply = await send(
      'native-roster',
      [{'role': 'user', 'content': 'Reply briefly without using any tools.'}],
      tools=roster,
    )
    assert roster_reply['choices'][0]['finish_reason'] == 'stop', roster_reply
    assert roster
    captures['native-roster-tool-count'] = len(roster)

    without_reasoning = deepcopy(second_messages)
    for message in without_reasoning:
      message.pop('reasoning_content', None)
    try:
      missing = await send('missing-reasoning', without_reasoning, tools=tools)
    except openai.APIStatusError as error:
      assert error.status_code == 400
    else:
      assert missing['choices'][0]['finish_reason'] in {'stop', 'tool_calls'}, missing

    off_history = [{'role': 'user', 'content': 'Say hello briefly.'}]
    off = await send('thinking-off', off_history, options={'thinking': {'type': 'disabled'}})
    assert off['choices'][0]['finish_reason'] == 'stop', off
    off_history.append(assistant_message(off))
    off_history.append({'role': 'user', 'content': 'Say goodbye briefly.'})
    try:
      on = await send('thinking-off-to-on', off_history, tools=tools)
    except openai.APIStatusError as error:
      assert error.status_code == 400
    else:
      assert on['choices'][0]['finish_reason'] == 'stop', on

    limited = await send(
      'forced-length',
      [{'role': 'user', 'content': 'Write a long essay about prime numbers.'}],
      max_tokens=1,
    )
    assert limited['choices'][0]['finish_reason'] == 'length', limited
  reporter = request.config.pluginmanager.get_plugin('terminalreporter')
  reporter.write_line(f'DeepSeek raw responses: {tmp_path / "deepseek-responses.json"}')
