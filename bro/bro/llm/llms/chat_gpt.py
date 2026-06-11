import dataclasses
from dataclasses import dataclass
from typing import ClassVar, Literal, Self, cast, get_args

import llm.llm
import configs
from llm.mcp import MCPServer, Tool, ToolControlSignal
from llm.observer import Observer
from llm.tracker import Tracker

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

ServiceTier = Literal['auto', 'default', 'flex', 'priority']
_VALID_SERVICE_TIERS: frozenset[str] = frozenset(get_args(ServiceTier))
# openai exports ReasoningEffort as Optional[Literal[...]], so unwrap the inner
# Literal before flattening to a set of valid string values.
_VALID_REASONING_EFFORTS: frozenset[str] = frozenset(get_args(get_args(ReasoningEffort)[0]))


@dataclass(frozen=True)
class LLMSpec(llm.llm.LLMSpec):
  """spec for the OpenAI Responses API.

  service_tier='priority' is the analog of Claude Code's /fast — same model
  and quality, higher per-token price, faster and more consistent generation.
  Toggle it through `.fast()` rather than constructing a new spec by hand.
  """

  TYPE: ClassVar[str] = 'chat_gpt'

  model: str = 'gpt-5'
  reasoning_effort: ReasoningEffort | None = None
  service_tier: ServiceTier | None = None

  def __post_init__(self):
    if self.service_tier is not None and self.service_tier not in _VALID_SERVICE_TIERS:
      raise ValueError(
        f'invalid service_tier {self.service_tier!r}; expected one of '
        f'{sorted(_VALID_SERVICE_TIERS)} or None'
      )
    if self.reasoning_effort is not None and self.reasoning_effort not in _VALID_REASONING_EFFORTS:
      raise ValueError(
        f'invalid reasoning_effort {self.reasoning_effort!r}; expected one of '
        f'{sorted(_VALID_REASONING_EFFORTS)} or None'
      )

  def fast(self) -> Self:
    return dataclasses.replace(self, service_tier='priority')

  def create_llm(
    self,
    mcp_servers: list[MCPServer] | None = None,
    observer: Observer | None = None,
    tracker: Tracker | None = None,
  ) -> llm.llm.LLM:
    return ChatGPT.create(
      model=self.model,
      reasoning_effort=self.reasoning_effort,
      service_tier=self.service_tier,
      mcp_servers=mcp_servers,
      observer=observer,
      tracker=tracker,
    )

  def dump(self) -> dict:
    return {
      'type': self.TYPE,
      'model': self.model,
      'reasoning_effort': self.reasoning_effort,
      'service_tier': self.service_tier,
    }

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    # __post_init__ revalidates these against the Literal types; the cast keeps
    # the static checker happy on the JSON-derived path where pyright sees
    # `str | None`.
    return cls(
      model=data['model'],
      reasoning_effort=cast(ReasoningEffort | None, data.get('reasoning_effort')),
      service_tier=cast(ServiceTier | None, data.get('service_tier')),
    )


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
    observer: Observer | None = None,
    tracker: Tracker | None = None,
  ):
    with open(config_path, 'r') as f:
      config = json.load(f)
    return ChatGPT(
      api_key=config['api_key'],
      model=model,
      mcp_servers=mcp_servers,
      reasoning_effort=reasoning_effort,
      service_tier=service_tier,
      observer=observer,
      tracker=tracker,
    )

  def __init__(
    self,
    api_key: str,
    model: str = 'gpt-5',
    mcp_servers: list[MCPServer] | None = None,
    reasoning_effort: ReasoningEffort | None = None,
    service_tier: str | None = None,
    observer: Observer | None = None,
    tracker: Tracker | None = None,
  ):
    super().__init__(mcp_servers, observer=observer, tracker=tracker)
    self.model = model
    self.client = OpenAI(api_key=api_key)
    self._openai_tools: list[ToolParam] | None = None
    self._last_response_id: str | None = None
    self._reasoning_effort = reasoning_effort
    self._service_tier = service_tier
    # round-trip counter shared across the whole trail: turn 0 holds the
    # framework's system_prompt step (auto-emitted by tracker.start_trail) and
    # the user_input we emit at the top of send(); each subsequent
    # responses.create increments it before emitting the llm_call + per-output
    # steps it produces.
    self._turn_index: int = 0
    # client-side fork seam: bro.fork sets this to the replayed conversation
    # prefix (already in OpenAI input shape — system message at index 0, user
    # messages, model output items, function_call_outputs). consumed on the
    # first send: prepended to the API input and a system message in the
    # incoming messages list is dropped (the prefix already carries the
    # system). cleared after one use so subsequent send()s behave normally.
    self._input_prefix: list[ResponseInputItemParam] | None = None

  async def _resolve_openai_tools(self) -> list[ToolParam]:
    if self._openai_tools is not None:
      return self._openai_tools
    tools = await self.tools.resolve()
    self._openai_tools = tools_to_openai_format(tools)
    return self._openai_tools

  def _emit_response_steps(self, response: Response, *, is_terminal: bool, turn_index: int) -> None:
    # walk output in order so both the live observer trace and the recorded
    # trail mirror the model's own sequence of reasoning, assistant text, and
    # tool calls. tool *results* are emitted by _execute_tool_calls after the
    # call returns. assistant text carries the `terminal` flag so the observer
    # can tell mid-stream chatter (between tool calls) apart from the final
    # reply (the same text LLM.send returns) — callers that already render the
    # return value can branch on it; on the trail it lands as an extras field.
    for item in response.output:
      if item.type == 'reasoning':
        for part in item.summary:
          if part.type == 'summary_text' and len(part.text) > 0:
            self.observer.on_reasoning(part.text)
            self.tracker.step('reasoning', part.text, turn_index=turn_index)
      elif item.type == 'message':
        text = ''.join(c.text for c in item.content if c.type == 'output_text')
        if len(text) > 0:
          self.observer.on_assistant_message(text, terminal=is_terminal)
          self.tracker.step('assistant', text, turn_index=turn_index, terminal=is_terminal)
      elif item.type == 'function_call':
        try:
          args = json.loads(item.arguments)
        except json.JSONDecodeError:
          args = {'_raw_arguments': item.arguments}
        self.observer.on_tool_call(item.name, args)
        self.tracker.step(
          'tool_call',
          None,
          turn_index=turn_index,
          tool_name=item.name,
          arguments=args,
          call_id=item.call_id,
        )

  async def _execute_tool_calls(
    self, response: Response, *, turn_index: int
  ) -> list[ResponseInputItemParam]:
    results: list[ResponseInputItemParam] = []
    for item in response.output:
      if item.type != 'function_call':
        continue
      kwargs = json.loads(item.arguments)
      is_error = False
      try:
        output = await self.tools.call(item.name, kwargs)
      except ToolControlSignal:
        raise
      except Exception as exc:
        # surface the failure back to the model as the tool result so the agent
        # can react (retry, switch source, raise) instead of crashing the loop.
        output = f'tool {item.name!r} failed: {type(exc).__name__}: {exc}'
        is_error = True
      self.observer.on_tool_result(item.name, output)
      # tracker body keeps the raw tool output (dict or str) — the JSON encoding
      # we do below for the API is a wire-format concern only.
      self.tracker.step(
        'tool_result',
        output,
        turn_index=turn_index,
        tool_name=item.name,
        call_id=item.call_id,
        is_error=is_error,
      )
      if isinstance(output, dict):
        output = json.dumps(output)
      results.append({'type': 'function_call_output', 'call_id': item.call_id, 'output': output})
    return results

  def _reasoning_kwargs(self) -> dict:
    if self._reasoning_effort is None:
      return {}
    # `summary='auto'` is what gives us the inner monologue. it's free (not
    # billed as output tokens) and only renders when the model actually has
    # something worth summarising. `include=['reasoning.encrypted_content']`
    # asks the server to return its raw (encrypted) reasoning items in the
    # response payload — captured into the llm_call body so client-side replay
    # past the response_id TTL stays high-fidelity for reasoning models.
    return {
      'reasoning': {'effort': self._reasoning_effort, 'summary': 'auto'},
      'include': ['reasoning.encrypted_content'],
    }

  def _service_tier_kwargs(self) -> dict:
    # 'priority' trades a higher per-token price for faster, more consistent
    # generation at the same model/quality — the analog of Claude Code's /fast.
    if self._service_tier is None:
      return {}
    return {'service_tier': self._service_tier}

  def _record_llm_call(self, request: dict, response: Response, *, turn_index: int) -> None:
    # raw request + response payload lands inline in the trail step body. for
    # LocalFileTracker that's a fat JSONL line; HTTPTracker will spill anything
    # over the inline threshold to S3 server-side and replace the body with
    # `{"s3": <key>}` — the bro doesn't know the difference.
    body = {'request': request, 'response': response.model_dump(mode='json')}
    usage = getattr(response, 'usage', None)
    tokens_in = getattr(usage, 'input_tokens', 0) if usage is not None else 0
    tokens_out = getattr(usage, 'output_tokens', 0) if usage is not None else 0
    details = getattr(usage, 'output_tokens_details', None) if usage is not None else None
    tokens_reasoning = getattr(details, 'reasoning_tokens', 0) if details is not None else 0
    self.tracker.step(
      'llm_call',
      body,
      turn_index=turn_index,
      response_id=response.id,
      tokens_in=tokens_in,
      tokens_out=tokens_out,
      tokens_reasoning=tokens_reasoning,
    )

  def _build_request_kwargs(
    self,
    input_items: list[ResponseInputItemParam],
    openai_tools: list[ToolParam],
    *,
    previous_response_id: str | None,
  ) -> dict:
    kwargs: dict = {
      'model': self.model,
      'input': input_items,
      'tools': openai_tools,
      **self._reasoning_kwargs(),
      **self._service_tier_kwargs(),
    }
    if previous_response_id is not None:
      kwargs['previous_response_id'] = previous_response_id
    return kwargs

  def _create(self, request_kwargs: dict, request_timeout: float | None) -> Response:
    # per-request timeout overrides the client default (the OpenAI SDK's 600s)
    # for this call; None leaves that default in place. responses.create is
    # non-streaming — no bytes return until generation finishes — so without a
    # tighter bound a stalled request blocks the full client timeout before the
    # SDK's automatic retry fires.
    if request_timeout is not None:
      return self.client.responses.create(**request_kwargs, timeout=request_timeout)
    return self.client.responses.create(**request_kwargs)

  async def send(self, messages: list[dict], *, request_timeout: float | None = None) -> str:
    openai_tools = await self._resolve_openai_tools()
    # client-side fork: the replayed prefix passes through unconverted (it is
    # already in OpenAI input shape, mixing role-keyed messages with raw output
    # items and function_call_outputs). the incoming system message is dropped
    # because the prefix carries its own system at index 0. the prefix is
    # consumed exactly once.
    api_input: list[ResponseInputItemParam] = []
    incoming = messages
    if self._input_prefix is not None:
      api_input.extend(self._input_prefix)
      incoming = [msg for msg in messages if msg.get('role') != 'system']
      self._input_prefix = None
    api_input.extend(convert_message(msg) for msg in incoming)

    # user_input shares turn 0 with the auto-emitted system_prompt on the very
    # first send(). on later send()s (interactive multi-turn) advance to a
    # fresh turn so the new user_input doesn't share a turn_index with the
    # previous turn's llm_call. system messages are skipped — start_trail
    # already emitted the system_prompt step.
    user_messages = [msg for msg in incoming if msg.get('role') == 'user']
    if len(user_messages) > 0 and self._turn_index > 0:
      self._turn_index += 1
    for msg in user_messages:
      self.tracker.step('user_input', _extract_text(msg), turn_index=self._turn_index)

    request_kwargs = self._build_request_kwargs(
      api_input, openai_tools, previous_response_id=self._last_response_id
    )
    self._turn_index += 1
    response = self._create(request_kwargs, request_timeout)
    self._record_llm_call(request_kwargs, response, turn_index=self._turn_index)
    self._emit_response_steps(
      response, is_terminal=not has_tool_calls(response), turn_index=self._turn_index
    )

    while has_tool_calls(response):
      tool_results = await self._execute_tool_calls(response, turn_index=self._turn_index)
      request_kwargs = self._build_request_kwargs(
        tool_results, openai_tools, previous_response_id=response.id
      )
      self._turn_index += 1
      response = self._create(request_kwargs, request_timeout)
      self._record_llm_call(request_kwargs, response, turn_index=self._turn_index)
      self._emit_response_steps(
        response, is_terminal=not has_tool_calls(response), turn_index=self._turn_index
      )

    self._last_response_id = response.id
    return parse_response(response)


def _extract_text(msg: dict) -> str:
  # body for the user_input step. multimodal content (images, files) is
  # discarded here; the full structured input still lives on the next
  # llm_call step's request payload.
  content = msg.get('content', '')
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    return '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
  return str(content)
