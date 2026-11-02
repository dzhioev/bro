from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from openai import AsyncOpenAI, pydantic_function_tool
from pydantic import BaseModel

from bro.base import log
from bro.llm.llm import LLMStopped, Recipe
from bro.llm.mcp import Tool, encode_tool_result
from bro.llm.records import Chaining, DecodedCall, Unknown
from bro.llm.retry import retry_llm
from bro.llm.wire import (
  Content,
  Input,
  Leg,
  Notification,
  Prefix,
  SystemPrompt,
  Text,
  ToolOutput,
  UnusableResponse,
  UserMessage,
  Wire,
)
from bro.wire_responses.content import encode_content
from bro.wire_responses.decode import CHAINING, NAME, counts, decode

if TYPE_CHECKING:
  from openai.types.responses import (
    Response,
    ToolParam,
  )


def parse_response(response: dict, decoded: DecodedCall | None = None) -> str:
  decoded = decode(response) if decoded is None else decoded
  for item in response['output']:
    if item['type'] != 'message' or item.get('phase') == 'commentary':
      continue
    for part in item['content']:
      if part['type'] == 'refusal':
        raise RuntimeError(f'got refusal: {part["refusal"]}')
      if part['type'] != 'output_text' or not isinstance(part.get('text'), str):
        raise RuntimeError(f'unreadable reply content: {part}')
  if decoded.reply is None:
    raise RuntimeError(f'output holds tool calls, not a reply: {response["output"]}')
  unreadable = [
    part.raw for part in decoded.parts if isinstance(part, Unknown) and not _is_compaction(part.raw)
  ]
  if not decoded.reply and unreadable:
    raise RuntimeError(f'unreadable output without reply text: {unreadable}')
  return decoded.reply


def _is_compaction(item: dict) -> bool:
  return item['type'] == 'compaction'


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


class Responses(Wire):
  NAME = NAME
  CHAINING: ClassVar[Chaining] = CHAINING
  OWNED = frozenset({'model', 'input', 'tools', 'previous_response_id'})

  def __init__(self, recipe: Recipe, secrets: dict):
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
    return {'role': 'user', 'content': encode_content(self.recipe, item.content)}

  async def exchange(
    self, inputs: list[Input], tools: list[Tool], request_timeout: float | None
  ) -> Leg:
    new_items = [self._encode(item) for item in inputs]
    request: dict = {
      'model': self.recipe.model,
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
        parse_response(raw, decoded)
      except Exception as error:
        if not leg.reply:
          raise UnusableResponse(str(error), leg, accepted=True) from error
        log.warning('reply extraction failed, falling back to the terminal message text: %s', error)
    return leg

  async def structured[T: BaseModel](
    self,
    instructions: str,
    result: type[T],
    content: tuple[Content, ...],
    request_timeout: float | None,
  ) -> T:
    async with self.client:
      function = pydantic_function_tool(result)['function']
      schema = function.get('parameters')
      assert schema is not None
      request = {
        **self.recipe.request_options(),
        'model': self.recipe.model,
        'input': [self._encode(SystemPrompt(instructions)), self._encode(UserMessage(content))],
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
        items.append(self._encode(UserMessage((Text(step.body),))))
      elif step.kind == 'llm_call' and 'stopped' not in step.extras:
        for item in step.body['response']['output']:
          if not _is_compaction(item):
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
