import llm.llm
import configs
from llm.mcp import MCPServer, Tool, ToolControlSignal
from llm.tracer import Tracer

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
from openai.types.responses.response_input_file_param import ResponseInputFileParam
from openai.types.responses.response_input_image_param import ResponseInputImageParam
from openai.types.responses.response_input_text_param import ResponseInputTextParam
from openai.types.shared import ReasoningEffort

ResponseInputContentPart = ResponseInputContentParam

import json
import os
import base64

DEFAULT_CONFIG_PATH = os.path.join(configs.DEFAULT_CONFIGS_DIR, 'openai.json')


def encode_file(path: str) -> str:
  with open(path, 'rb') as f:
    return base64.b64encode(f.read()).decode('utf-8')


def image_to_content(data: bytes, mime_type: str) -> ResponseInputImageParam:
  encoded = base64.b64encode(data).decode('utf-8')
  image_url = f'data:{mime_type};base64,{encoded}'
  return ResponseInputImageParam(type='input_image', image_url=image_url, detail='high')


def png_to_content(data: bytes) -> ResponseInputImageParam:
  return image_to_content(data, 'image/png')


def image_file_to_content(image_path: str) -> ResponseInputImageParam:
  if not image_path.endswith('.png'):
    raise NotImplementedError('only PNG images supported')
  with open(image_path, 'rb') as f:
    return png_to_content(f.read())


def pdf_to_content(data: bytes, filename: str) -> ResponseInputFileParam:
  encoded = base64.b64encode(data).decode('utf-8')
  # OpenAI's input_file rejects filenames containing path separators (e.g.
  # "Payslip4/2026.pdf" comes back as 400 "badly formatted or corrupted").
  safe_filename = filename.replace('/', '_').replace('\\', '_') or 'file.pdf'
  return ResponseInputFileParam(
    type='input_file', file_data=f'data:application/pdf;base64,{encoded}', filename=safe_filename
  )


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


def tools_to_openai_format(tools: list[Tool]) -> list[ToolParam]:
  # strict mode is intentionally off: it forces every property into `required`,
  # which makes pydantic-`default=None` fields un-omittable and traps the model
  # into clobbering them with default-looking values on every call. With
  # strict=False the schema flows through as pydantic generated it and optional
  # fields stay genuinely optional.
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
  def create(
    config_path=DEFAULT_CONFIG_PATH,
    model: str = 'gpt-5',
    mcp_servers: list[MCPServer] | None = None,
    reasoning_effort: ReasoningEffort | None = None,
    service_tier: str | None = None,
    tracer: Tracer | None = None,
  ):
    with open(config_path, 'r') as f:
      config = json.load(f)
    return ChatGPT(
      api_key=config['api_key'],
      model=model,
      mcp_servers=mcp_servers,
      reasoning_effort=reasoning_effort,
      service_tier=service_tier,
      tracer=tracer,
    )

  def __init__(
    self,
    api_key: str,
    model: str = 'gpt-5',
    mcp_servers: list[MCPServer] | None = None,
    reasoning_effort: ReasoningEffort | None = None,
    service_tier: str | None = None,
    tracer: Tracer | None = None,
  ):
    super().__init__(mcp_servers, tracer=tracer)
    self.model = model
    self.client = OpenAI(api_key=api_key)
    self._openai_tools: list[ToolParam] | None = None
    self._last_response_id: str | None = None
    self._reasoning_effort = reasoning_effort
    self._service_tier = service_tier

  async def _resolve_openai_tools(self) -> list[ToolParam]:
    if self._openai_tools is not None:
      return self._openai_tools
    tools = await self.tools.resolve()
    self._openai_tools = tools_to_openai_format(tools)
    return self._openai_tools

  def _emit_response_events(self, response: Response, *, is_terminal: bool) -> None:
    # walk output in order so the trace mirrors the model's own sequence of
    # reasoning, interim messages, and tool calls. tool *results* are emitted
    # by _execute_tool_calls after the call returns. on the terminal response
    # (no more tool calls), the assistant message text IS the return value of
    # send() — skip it here so callers don't see it twice.
    for item in response.output:
      if item.type == 'reasoning':
        for part in item.summary:
          if part.type == 'summary_text' and len(part.text) > 0:
            self.tracer.on_reasoning(part.text)
      elif item.type == 'message':
        if is_terminal:
          continue
        text = ''.join(c.text for c in item.content if c.type == 'output_text')
        if len(text) > 0:
          self.tracer.on_assistant_message(text)
      elif item.type == 'function_call':
        try:
          args = json.loads(item.arguments)
        except json.JSONDecodeError:
          args = {'_raw_arguments': item.arguments}
        self.tracer.on_tool_call(item.name, args)

  async def _execute_tool_calls(self, response: Response) -> list[ResponseInputItemParam]:
    results: list[ResponseInputItemParam] = []
    for item in response.output:
      if item.type != 'function_call':
        continue
      kwargs = json.loads(item.arguments)
      try:
        output = await self.tools.call(item.name, kwargs)
      except ToolControlSignal:
        raise
      except Exception as exc:
        # surface the failure back to the model as the tool result so the agent
        # can react (retry, switch source, raise) instead of crashing the loop.
        output = f'tool {item.name!r} failed: {type(exc).__name__}: {exc}'
      self.tracer.on_tool_result(item.name, output)
      if isinstance(output, dict):
        output = json.dumps(output)
      results.append({'type': 'function_call_output', 'call_id': item.call_id, 'output': output})
    return results

  def _reasoning_kwargs(self) -> dict:
    if self._reasoning_effort is None:
      return {}
    # `summary='auto'` is what gives us the inner monologue. it's free (not
    # billed as output tokens) and only renders when the model actually has
    # something worth summarising.
    return {'reasoning': {'effort': self._reasoning_effort, 'summary': 'auto'}}

  def _service_tier_kwargs(self) -> dict:
    # 'priority' trades a higher per-token price for faster, more consistent
    # generation at the same model/quality — the analog of Claude Code's /fast.
    if self._service_tier is None:
      return {}
    return {'service_tier': self._service_tier}

  async def send(self, messages: list[dict]) -> str:
    openai_tools = await self._resolve_openai_tools()
    api_input: list[ResponseInputItemParam] = [convert_message(msg) for msg in messages]

    kwargs: dict = {
      'model': self.model,
      'input': api_input,
      'tools': openai_tools,
      **self._reasoning_kwargs(),
      **self._service_tier_kwargs(),
    }
    if self._last_response_id is not None:
      kwargs['previous_response_id'] = self._last_response_id

    response = self.client.responses.create(**kwargs)
    self._emit_response_events(response, is_terminal=not has_tool_calls(response))

    while has_tool_calls(response):
      tool_results = await self._execute_tool_calls(response)
      continuation_kwargs: dict = {
        'model': self.model,
        'previous_response_id': response.id,
        'input': tool_results,
        'tools': openai_tools,
        **self._reasoning_kwargs(),
        **self._service_tier_kwargs(),
      }
      response = self.client.responses.create(**continuation_kwargs)
      self._emit_response_events(response, is_terminal=not has_tool_calls(response))

    self._last_response_id = response.id
    return parse_response(response)
