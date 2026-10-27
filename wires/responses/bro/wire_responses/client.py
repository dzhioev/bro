from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar

from openai import AsyncOpenAI, pydantic_function_tool
from pydantic import BaseModel

from bro.base import log
from bro.llm.llm import LLMSpec, LLMStopped, UnsupportedContent
from bro.llm.mcp import Tool, encode_tool_result
from bro.llm.records import Chaining, DecodedCall
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
from bro.wire_responses.content import image_file_to_content, text_to_content
from bro.wire_responses.decode import CHAINING, NAME, counts, decode

if TYPE_CHECKING:
  from openai.types.responses import (
    Response,
    ToolParam,
  )
  from openai.types.responses.easy_input_message_param import EasyInputMessageParam
  from openai.types.responses.response_input_content_param import ResponseInputContentParam


def parse_response(response: dict, decoded: DecodedCall | None = None) -> str:
  decoded = decode(response) if decoded is None else decoded
  messages = [
    item
    for item in response['output']
    if item['type'] == 'message' and item.get('phase') != 'commentary'
  ]
  if not messages:
    raise RuntimeError(f"output doesn't contain reply messages. output: {response['output']}")
  for message in messages:
    for item in message['content']:
      if item['type'] == 'refusal':
        raise RuntimeError(f'got refusal: {item["refusal"]}')
      assert item['type'] == 'output_text'
  if decoded.reply is None:
    raise RuntimeError(f'no output texts in messages: {messages}')
  return decoded.reply


def tools_to_openai_format(tools: list[Tool]) -> list[ToolParam]:
  # Strict schemas require even optional properties, preventing omission of defaulted fields.
  from openai.types.responses.function_tool_param import FunctionToolParam

  result: list[ToolParam] = []
  for tool in tools:
    result.append(
      FunctionToolParam(
        type='function',
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
        strict=False,
      )
    )
  return result


def convert_content_part(part: dict) -> ResponseInputContentParam:
  from openai.types.responses.response_input_image_param import ResponseInputImageParam

  if part.get('type') == 'text':
    return text_to_content(part['text'])
  if part.get('type') == 'image_url':
    url = part.get('image_url', {}).get('url', '')
    if url.startswith('data:'):
      return ResponseInputImageParam(type='input_image', image_url=url, detail='high')
    return image_file_to_content(url)
  raise UnsupportedContent(f'unsupported Responses content part {part.get("type")!r}')


def convert_message(msg: dict) -> EasyInputMessageParam:
  from openai.types.responses.easy_input_message_param import EasyInputMessageParam

  role = msg.get('role', 'user')
  content = msg.get('content', '')
  if isinstance(content, list):
    converted: list[ResponseInputContentParam] = [convert_content_part(p) for p in content]
    return EasyInputMessageParam(role=role, content=converted)
  return EasyInputMessageParam(
    role=role, content=content if isinstance(content, str) else str(content)
  )


class Responses(Wire):
  NAME = NAME
  CHAINING: ClassVar[Chaining] = CHAINING
  OWNED = frozenset({'model', 'input', 'tools', 'previous_response_id'})

  def __init__(self, recipe: LLMSpec, secrets: dict):
    self.recipe = recipe
    self.client = AsyncOpenAI(api_key=secrets['api_key'], base_url=recipe.ENDPOINT)
    self._last_response_id: str | None = None
    self._input_prefix: list[dict] = []

  def _encode(self, item: Input) -> dict:
    if isinstance(item, SystemPrompt):
      return {'role': 'system', 'content': item.text}
    if isinstance(item, Notification):
      return {'role': 'user', 'content': item.text}
    if isinstance(item, ToolOutput):
      return {
        'type': 'function_call_output',
        'call_id': item.call_id,
        'output': encode_tool_result(item.output),
      }
    content = item.content
    if isinstance(content, list):
      for part in content:
        kind = 'image' if part['type'] == 'image_url' else part['type']
        if not self.recipe.accepts(kind):
          raise UnsupportedContent(
            f'{kind} content is not supported by {self.recipe.selection()["model"]}'
          )
    elif not self.recipe.accepts('text'):
      raise UnsupportedContent(
        f'text content is not supported by {self.recipe.selection()["model"]}'
      )
    return dict(convert_message({'role': 'user', 'content': content}))

  async def exchange(
    self, inputs: list[Input], tools: list[Tool], request_timeout: float | None
  ) -> Leg:
    new_items = [self._encode(item) for item in inputs]
    request: dict = {
      'model': self.recipe.selection()['model'],
      'input': self._input_prefix + new_items,
      'tools': tools_to_openai_format(tools),
      **self.recipe.request_options(),
    }
    if self._last_response_id is not None:
      request['previous_response_id'] = self._last_response_id

    async def create() -> Response:
      if request_timeout is not None:
        return await self.client.responses.create(**request, timeout=request_timeout)
      return await self.client.responses.create(**request)

    response = await retry_llm(create, self.recipe.is_transient)
    raw = response.model_dump(mode='json')
    decoded = decode(raw)
    leg = Leg.from_decoded(
      {'request': {**request, 'input': new_items}, 'response': raw},
      {'response_id': response.id},
      self.NAME,
      decoded,
      counts(decoded.usage) if raw.get('usage') is not None else None,
    )
    if leg.stop != 'completed':
      return leg
    for call in leg.calls:
      if not call.call_id or not call.name:
        raise RuntimeError('tool call missing its id or name')
    self._last_response_id = response.id
    self._input_prefix = []
    if not leg.calls:
      try:
        reply = parse_response(raw, decoded)
      except Exception as error:
        if decoded.reply is None:
          raise UnusableResponse(str(error), leg) from error
        log.warning('reply extraction failed, falling back to the terminal message text: %s', error)
        reply = decoded.reply
      leg = replace(leg, reply=reply)
    return leg

  async def structured[T: BaseModel](
    self, prompt: str, result: type[T], contents: list, request_timeout: float | None
  ) -> T:
    async with self.client:
      function = pydantic_function_tool(result)['function']
      schema = function.get('parameters')
      assert schema is not None
      request = {
        **self.recipe.request_options(),
        'model': self.recipe.selection()['model'],
        'input': [self._encode(SystemPrompt(prompt)), self._encode(UserMessage(contents))],
        'text': {
          'format': {
            'type': 'json_schema',
            'name': function['name'],
            'schema': schema,
            'strict': True,
          }
        },
      }
      if request_timeout is not None:
        request['timeout'] = request_timeout
      response = await retry_llm(
        lambda: self.client.responses.create(**request),
        self.recipe.is_transient,
        capacity=lambda response: decode(response.model_dump(mode='json')).stop == 'capacity',
      )
    raw = response.model_dump(mode='json')
    decoded = decode(raw)
    if decoded.stop != 'completed':
      raise LLMStopped(decoded.stop, decoded.stop_reason)
    return result.model_validate_json(parse_response(raw, decoded))

  def restore(self, prefix: Prefix) -> None:
    items: list[dict] = [{'role': 'system', 'content': prefix.system_prompt}]
    for step in prefix.steps:
      if step.kind in {'user_input', 'notification'}:
        items.append({'role': 'user', 'content': step.body})
      elif step.kind == 'llm_call' and 'stopped' not in step.extras:
        for item in step.body['response']['output']:
          if item['type'] != 'compaction':
            items.append(_replay_item(item))
      elif step.kind == 'tool_result':
        items.append(self._encode(ToolOutput(step.extras['call_id'], step.body)))
    self._last_response_id = None
    self._input_prefix = items

  def seed_chain(self, response_id: str) -> None:
    self._last_response_id = response_id
    self._input_prefix = []


def _replay_item(value: Any) -> Any:
  # Responses input rejects response-only status fields and null optionals.
  if isinstance(value, dict):
    return {
      key: _replay_item(item) for key, item in value.items() if key != 'status' and item is not None
    }
  if isinstance(value, list):
    return [_replay_item(item) for item in value]
  return value
