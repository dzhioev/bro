import llm.llm
import configs
from llm.mcp import MCPServer, Tool

from openai import OpenAI
from openai.types.responses import (
  Response,
  ResponseInputItemParam,
  ResponseOutputItem,
  ResponseOutputMessage,
  ToolParam,
)
from openai.types.responses.easy_input_message_param import EasyInputMessageParam
from openai.types.responses.function_tool_param import FunctionToolParam
from openai.types.responses.response_input_content_param import ResponseInputContentParam
from openai.types.responses.response_input_image_param import ResponseInputImageParam
from openai.types.responses.response_input_text_param import ResponseInputTextParam

ResponseInputContentPart = ResponseInputContentParam

import json
import logging
import os
import base64

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(configs.DEFAULT_CONFIGS_DIR, 'openai.json')


def encode_file(path: str) -> str:
  with open(path, 'rb') as f:
    return base64.b64encode(f.read()).decode('utf-8')


def png_to_content(data: bytes) -> ResponseInputImageParam:
  encoded = base64.b64encode(data).decode('utf-8')
  image_url = f'data:image/png;base64,{encoded}'
  return ResponseInputImageParam(type='input_image', image_url=image_url, detail='high')


def image_file_to_content(image_path: str) -> ResponseInputImageParam:
  if not image_path.endswith('.png'):
    raise NotImplementedError('only PNG images supported')
  with open(image_path, 'rb') as f:
    return png_to_content(f.read())


def text_to_content(text: str) -> ResponseInputTextParam:
  return ResponseInputTextParam(type='input_text', text=text)


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


def _make_strict_schema(schema: dict) -> dict:
  result = dict(schema)
  if result.get('type') == 'object' and 'properties' in result:
    properties = {k: _make_strict_schema(v) for k, v in result['properties'].items()}
    result['properties'] = properties
    result['required'] = list(properties.keys())
    result['additionalProperties'] = False
  return result


def tools_to_openai_format(tools: list[Tool]) -> list[ToolParam]:
  result: list[ToolParam] = []
  for tool in tools:
    result.append(
      FunctionToolParam(
        type='function',
        name=tool.name,
        description=tool.description,
        parameters=_make_strict_schema(tool.parameters),
        strict=True,
      )
    )
  return result


def convert_content_part(part: dict) -> ResponseInputContentPart:
  if part.get('type') == 'text':
    return text_to_content(part['text'])
  if part.get('type') == 'image_url':
    url = part.get('image_url', {}).get('url', '')
    if url.startswith('data:'):
      return ResponseInputImageParam(type='input_image', image_url=url, detail='high')
    return image_file_to_content(url)
  return text_to_content(str(part))


def convert_message(msg: dict) -> EasyInputMessageParam:
  role = msg.get('role', 'user')
  content = msg.get('content', '')
  if isinstance(content, list):
    converted: list[ResponseInputContentParam] = [convert_content_part(p) for p in content]
    return EasyInputMessageParam(role=role, content=converted)
  return EasyInputMessageParam(
    role=role, content=content if isinstance(content, str) else str(content)
  )


class ChatGPT(llm.llm.LLM):
  @staticmethod
  def create(config_path=DEFAULT_CONFIG_PATH, mcp_servers: list[MCPServer] | None = None):
    with open(config_path, 'r') as f:
      config = json.load(f)
    return ChatGPT(api_key=config['api_key'], mcp_servers=mcp_servers)

  def __init__(self, api_key: str, mcp_servers: list[MCPServer] | None = None):
    self.client = OpenAI(api_key=api_key)
    self._mcp_servers: list[MCPServer] = list(mcp_servers or [])
    self._tools_by_name: dict[str, Tool] | None = None
    self._openai_tools: list[ToolParam] | None = None

  async def _resolve_tools(self) -> list[ToolParam]:
    if self._openai_tools is not None:
      assert self._tools_by_name is not None
      return self._openai_tools
    tools_by_name: dict[str, Tool] = {}
    for server in self._mcp_servers:
      for tool in await server.list_tools():
        if tool.name in tools_by_name:
          raise ValueError(f'duplicate tool name across MCP servers: {tool.name}')
        tools_by_name[tool.name] = tool
    self._tools_by_name = tools_by_name
    self._openai_tools = tools_to_openai_format(list(tools_by_name.values()))
    return self._openai_tools

  async def _execute_tool_calls(self, response: Response) -> list[ResponseInputItemParam]:
    assert self._tools_by_name is not None
    results: list[ResponseInputItemParam] = []
    for item in response.output:
      if item.type != 'function_call':
        continue
      tool = self._tools_by_name.get(item.name)
      if tool is None:
        output = f'unknown tool: {item.name}'
      else:
        kwargs = json.loads(item.arguments)
        log.info(f'calling tool {item.name} with {kwargs}')
        output = await tool.call(kwargs)
      results.append({'type': 'function_call_output', 'call_id': item.call_id, 'output': output})
    return results

  async def tell(self, messages: list[dict]) -> None:
    openai_tools = await self._resolve_tools()
    api_input: list[ResponseInputItemParam] = [convert_message(msg) for msg in messages]

    response = self.client.responses.create(
      model='gpt-5',
      input=api_input,
      tools=openai_tools,
    )

    while has_tool_calls(response):
      tool_results = await self._execute_tool_calls(response)
      response = self.client.responses.create(
        model='gpt-5',
        previous_response_id=response.id,
        input=tool_results,
        tools=openai_tools,
      )

    self.text_response = parse_response(response)

  async def ask(self) -> str:
    return self.text_response
