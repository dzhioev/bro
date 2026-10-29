from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import ClassVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from bro.llm.llm import LLMSpec, LLMStopped, UnsupportedContent
from bro.llm.mcp import Tool, encode_tool_result
from bro.llm.records import Chaining, Unknown
from bro.llm.retry import retry_llm
from bro.llm.wire import (
  Input,
  Leg,
  Notification,
  Prefix,
  SystemPrompt,
  ToolOutput,
  UnusableResponse,
  UserMessage,
  Wire,
)
from bro.wire_chat.decode import CHAINING, NAME, counts, decode


def tools_to_chat_format(tools: list[Tool]) -> list[dict]:
  return [
    {
      'type': 'function',
      'function': {
        'name': tool.name,
        'description': tool.description,
        'parameters': tool.parameters,
      },
    }
    for tool in tools
  ]


def assistant_message(response: dict) -> dict:
  message = response['choices'][0]['message']
  calls = message.get('tool_calls')
  content = message.get('content')
  # Chat Completions requires an assistant message's content unless it carries tool calls.
  result = {'role': 'assistant', 'content': '' if content is None and not calls else content}
  if message.get('reasoning_content') is not None:
    result['reasoning_content'] = message['reasoning_content']
  if calls:
    result['tool_calls'] = [
      {
        'id': call['id'],
        'type': call['type'],
        'function': {'name': call['function']['name'], 'arguments': call['function']['arguments']},
      }
      for call in calls
    ]
  return result


class Chat(Wire):
  NAME = NAME
  CHAINING: ClassVar[Chaining] = CHAINING
  OWNED = frozenset({'model', 'messages', 'tools'})

  def __init__(self, recipe: LLMSpec, secrets: dict):
    self.recipe = recipe
    self.client = AsyncOpenAI(api_key=secrets['api_key'], base_url=recipe.ENDPOINT, max_retries=0)
    self._history: list[dict] = []

  def _encode(self, item: Input) -> dict:
    if isinstance(item, SystemPrompt):
      return {'role': 'system', 'content': item.text}
    if isinstance(item, Notification):
      return {'role': 'user', 'content': item.text}
    if isinstance(item, ToolOutput):
      return {
        'role': 'tool',
        'tool_call_id': item.call_id,
        'content': encode_tool_result(item.output),
      }
    content = item.content
    if isinstance(content, str):
      self._check_content('text')
      return {'role': 'user', 'content': content}
    parts = []
    for part in content:
      kind = part['type']
      self._check_content('image' if kind == 'image_url' else kind)
      if kind == 'text':
        parts.append({'type': 'text', 'text': part['text']})
      elif kind == 'image_url':
        url = part['image_url']['url']
        if not url.startswith(('data:', 'https://', 'http://')):
          media_type = mimetypes.guess_type(url)[0]
          if media_type is None or not media_type.startswith('image/'):
            raise UnsupportedContent(f'unknown image media type: {url}')
          encoded = base64.b64encode(Path(url).read_bytes()).decode('ascii')
          url = f'data:{media_type};base64,{encoded}'
        parts.append({'type': 'image_url', 'image_url': {'url': url}})
      else:
        raise UnsupportedContent(f'unsupported Chat content part {kind!r}')
    return {'role': 'user', 'content': parts}

  def _check_content(self, kind: str) -> None:
    if not self.recipe.accepts(kind):
      raise UnsupportedContent(
        f'{kind} content is not supported by {self.recipe.selection()["model"]}'
      )

  async def _create(self, request: dict, request_timeout: float | None):
    # Non-standard protocol fields must reach the JSON body, not the SDK signature.
    options = dict(request)
    known = {'model', 'messages', 'tools', 'response_format'}
    extra = {key: options.pop(key) for key in list(options) if key not in known}
    if extra:
      options['extra_body'] = extra
    if not options.get('tools'):
      options.pop('tools', None)
    if request_timeout is not None:
      options['timeout'] = request_timeout
    return await self.client.chat.completions.create(**options)

  async def exchange(
    self, inputs: list[Input], tools: list[Tool], request_timeout: float | None
  ) -> Leg:
    new_items = [self._encode(item) for item in inputs]
    if not self._history:
      new_items = [item for item in new_items if item['role'] == 'system'] + [
        item for item in new_items if item['role'] != 'system'
      ]
    request = {
      **self.recipe.request_options(),
      'model': self.recipe.selection()['model'],
      'messages': self._history + new_items,
      'tools': tools_to_chat_format(tools),
    }
    response = await retry_llm(
      lambda: self._create(request, request_timeout), self.recipe.is_transient
    )
    raw = response.model_dump(mode='json')
    decoded = decode(raw)
    leg = Leg.from_decoded(
      {'request': {**request, 'messages': new_items}, 'response': raw},
      {'response_id': response.id} if response.id is not None else {},
      self.NAME,
      decoded,
      counts(decoded.usage) if raw.get('usage') is not None else None,
    )
    if leg.stop != 'completed':
      return leg
    if any(isinstance(part, Unknown) for part in leg.parts):
      raise UnusableResponse('unusable completed Chat response', leg, accepted=False)
    if not leg.calls and not leg.reply and leg.refusal is not None:
      raise UnusableResponse(f'completed Chat response refused: {leg.refusal}', leg, accepted=False)
    self._history = request['messages'] + [assistant_message(raw)]
    return leg

  def restore(self, prefix: Prefix) -> None:
    items = [self._encode(SystemPrompt(prefix.system_prompt))]
    for step in prefix.steps:
      if step.kind in {'user_input', 'notification'}:
        items.append(self._encode(UserMessage(step.body)))
      elif step.kind == 'llm_call' and 'stopped' not in step.extras:
        items.append(assistant_message(step.body['response']))
      elif step.kind == 'tool_result':
        items.append(self._encode(ToolOutput(step.extras['call_id'], step.body)))
    self._history = items

  async def structured[T: BaseModel](
    self, prompt: str, result: type[T], contents: list, request_timeout: float | None
  ) -> T:
    schema = json.dumps(result.model_json_schema(), ensure_ascii=False)
    request = {
      **self.recipe.request_options(),
      'model': self.recipe.selection()['model'],
      'messages': [
        self._encode(SystemPrompt(f'{prompt}\nReturn JSON matching this schema:\n{schema}')),
        self._encode(UserMessage(contents)),
      ],
      'response_format': {'type': 'json_object'},
    }
    async with self.client:
      response = await retry_llm(
        lambda: self._create(request, request_timeout),
        self.recipe.is_transient,
        capacity=lambda response: decode(response.model_dump(mode='json')).stop == 'capacity',
      )
    decoded = decode(response.model_dump(mode='json'))
    if decoded.stop != 'completed':
      raise LLMStopped(decoded.stop, decoded.stop_reason)
    if not decoded.reply:
      raise RuntimeError('completed Chat response has no structured reply')
    return result.model_validate_json(decoded.reply)
