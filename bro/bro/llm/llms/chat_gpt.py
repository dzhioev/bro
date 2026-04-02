import llm.llm
import configs

from openai import OpenAI
from openai.types.responses import (
  Response,
  ResponseOutputItem,
  ResponseOutputMessage,
  ResponseInputParam,
)

from flow.mcp.tools import TOOLS, Tool

import json
import logging
import os
import base64

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(configs.DEFAULT_CONFIGS_DIR, 'openai.json')


def encode_file(path: str) -> str:
  with open(path, 'rb') as f:
    return base64.b64encode(f.read()).decode('utf-8')


def png_to_content(data: bytes) -> dict[str, str]:
  encoded = base64.b64encode(data).decode('utf-8')
  image_url = f'data:image/png;base64,{encoded}'
  return {'type': 'input_image', 'image_url': image_url, 'detail': 'high'}


def image_file_to_content(image_path: str) -> dict[str, str]:
  if not image_path.endswith('.png'):
    raise NotImplementedError('only PNG images supported')
  with open(image_path, 'rb') as f:
    return png_to_content(f.read())


def text_to_content(text: str) -> dict[str, str]:
  return {'type': 'input_text', 'text': text}


def extract_only_message(output: list[ResponseOutputItem]) -> ResponseOutputMessage:
  result = None
  for item in output:
    if item.type == 'message':
      if result is not None:
        raise NotImplementedError(
          f'output contains more then one output messages. output: {output}'
        )
      result = item
  if result is None:
    raise RuntimeError(f"output doesn't contain output messages. output: {output}")
  return result


def parse_response(response: Response) -> str:
  message = extract_only_message(response.output)
  chunks = []
  for item in message.content:
    if item.type == 'refusal':
      raise RuntimeError('got refusal: {item.refusal}')
    assert item.type == 'output_text'
    chunks.append(item.text)
  if len(chunks) == 0:
    raise RuntimeError('no output texts in message: {response.message}')
  return ''.join(chunks)


def has_tool_calls(response: Response) -> bool:
  return any(item.type == 'function_call' for item in response.output)


def execute_tool_calls(response: Response) -> list[dict]:
  results = []
  for item in response.output:
    if item.type != 'function_call':
      continue
    tool = TOOLS_BY_NAME.get(item.name)
    if tool is None:
      output = f'unknown tool: {item.name}'
    else:
      kwargs = json.loads(item.arguments)
      log.info(f'calling tool {item.name} with {kwargs}')
      output = tool.handler(**kwargs)
    results.append({'type': 'function_call_output', 'call_id': item.call_id, 'output': output})
  return results


def tools_to_openai_format(tools: list[Tool]) -> list[dict]:
  return [
    {
      'type': 'function',
      'name': tool.name,
      'description': tool.description,
      'parameters': tool.parameters,
      'strict': True,
    }
    for tool in tools
  ]


TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
OPENAI_TOOLS = tools_to_openai_format(TOOLS)


class ChatGPT(llm.llm.LLM):
  @staticmethod
  def create(config_path=DEFAULT_CONFIG_PATH):
    with open(config_path, 'r') as f:
      config = json.load(f)
    return ChatGPT(api_key=config['api_key'])

  def __init__(self, api_key: str):
    self.client = OpenAI(api_key=api_key)

  async def tell(self, phrase: str, images: list[str] | None) -> None:
    content = []
    if images is not None:
      for image_path in images:
        content.append(image_file_to_content(image_path))
    content.append(text_to_content(phrase))

    response = self.client.responses.create(
      model='gpt-5',
      input=[{'role': 'user', 'content': content}],
      tools=OPENAI_TOOLS,
    )

    while has_tool_calls(response):
      tool_results = execute_tool_calls(response)
      response = self.client.responses.create(
        model='gpt-5',
        previous_response_id=response.id,
        input=tool_results,
        tools=OPENAI_TOOLS,
      )

    self.text_response = parse_response(response)

  async def ask(self) -> str:
    return self.text_response
