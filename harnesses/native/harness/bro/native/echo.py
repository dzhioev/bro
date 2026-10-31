"""The diagnostic echo protocol and its client."""

from bro.llm.llm import LLMSpec, UnsupportedContent
from bro.llm.mcp import Tool
from bro.llm.records import Chaining, DecodedCall, Message
from bro.llm.wire import (
  Input,
  Leg,
  Notification,
  Prefix,
  SystemPrompt,
  Text,
  ToolOutput,
  UserMessage,
  Wire,
  require_accepted,
)

NAME = 'echo'
CHAINING: Chaining = 'client'


def decode(response: dict) -> DecodedCall:
  return DecodedCall(response['model'], {}, None, (Message(1, response['text']),), 'completed')


def counts(usage: dict) -> dict[str, int]:
  return dict.fromkeys(('input', 'cache_write', 'cache_read', 'output'), 0)


class Echo(Wire):
  NAME = NAME
  CHAINING = CHAINING
  OWNED = frozenset({'model', 'input', 'tools'})

  def __init__(self, recipe: LLMSpec, secrets: dict):
    self.recipe = recipe
    self.history: list[dict] = []

  async def exchange(
    self, inputs: list[Input], tools: list[Tool], request_timeout: float | None
  ) -> Leg:
    items = [self._encode(item) for item in inputs]
    request = {
      'model': self.recipe.selection()['model'],
      'input': self.history + items,
      'tools': [],
    }
    response = self._respond(request)
    record = {'request': {**request, 'input': items}, 'response': response}
    self.history = request['input'] + [{'role': 'assistant', 'text': response['text']}]
    return Leg.from_decoded(record, {}, self.NAME, decode(response), None)

  def _respond(self, request: dict) -> dict:
    items = request['input']
    return {'model': request['model'], 'text': items[-1]['text'] if items else ''}

  def _encode(self, item: Input) -> dict:
    if isinstance(item, SystemPrompt):
      return {'role': 'system', 'text': item.text}
    if isinstance(item, Notification):
      return {'role': 'user', 'text': item.text}
    if isinstance(item, ToolOutput):
      raise ValueError('echo cannot answer tool calls')
    require_accepted(self.recipe, item.content)
    if any(not isinstance(part, Text) for part in item.content):
      raise UnsupportedContent('echo cannot carry file content')
    text = '\n'.join(part.text for part in item.content if isinstance(part, Text))
    return {'role': 'user', 'text': text}

  def restore(self, prefix: Prefix) -> None:
    self.history = [self._encode(SystemPrompt(prefix.system_prompt))]
    for step in prefix.steps:
      if step.kind in {'user_input', 'notification'}:
        self.history.append(self._encode(UserMessage((Text(step.body),))))
      elif step.kind == 'llm_call' and 'stopped' not in step.extras:
        self.history.append({'role': 'assistant', 'text': step.body['response']['text']})
