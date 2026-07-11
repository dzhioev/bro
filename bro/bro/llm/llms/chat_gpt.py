import base64
import dataclasses
import json
from dataclasses import dataclass
from typing import ClassVar, Literal, Optional, Self, cast, get_args

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

import llm.llm
import usage
from base import credentials
from llm.mcp import MCPServer, Tool, ToolControlSignal
from llm.observer import Observer
from llm.tracker import Tracker

ResponseInputContentPart = ResponseInputContentParam

ServiceTier = Literal['auto', 'default', 'flex', 'priority']
_VALID_SERVICE_TIERS: frozenset[str] = frozenset(get_args(ServiceTier))
# openai exports ReasoningEffort as Optional[Literal[...]], so unwrap the inner
# Literal before flattening to a set of valid string values.
_VALID_REASONING_EFFORTS: frozenset[str] = frozenset(get_args(get_args(ReasoningEffort)[0]))

# neutral effort level (`LLMSpec.with_effort`) → Responses API reasoning_effort.
# the shared levels map through; max (above the API's scale) caps at its top.
_EFFORT_TO_REASONING_EFFORT: dict[str, ReasoningEffort] = {
  'low': 'low',
  'medium': 'medium',
  'high': 'high',
  'xhigh': 'xhigh',
  'max': 'xhigh',
}


@dataclass(frozen=True)
class LLMSpec(llm.llm.LLMSpec):
  """spec for the OpenAI Responses API.

  service_tier='priority' is the analog of Claude Code's /fast — same model
  and quality, higher per-token price, faster and more consistent generation.
  Toggle it through `.fast()` rather than constructing a new spec by hand.

  compact_threshold (opt-in) bounds context growth in long runs: when the
  chained conversation crosses it, the server compacts the context in-band
  (see `ChatGPT._context_management_kwargs`). None (the default) leaves growth
  unbounded. GPT-5-family models take at most 272k input tokens (400k window
  minus the 128k output reservation), so a value like 200_000 leaves tool-loop
  turns room to grow between the threshold crossing and the compaction pass.
  Size it far above per-turn growth: with the threshold near the working
  context size the server recompacts repeatedly within one response (observed
  live: 10 passes per call, ~5x billed input, minutes of latency).
  """

  TYPE: ClassVar[str] = 'chat_gpt'

  model: str = 'gpt-5'
  reasoning_effort: Optional[ReasoningEffort] = None
  service_tier: Optional[ServiceTier] = None
  compact_threshold: Optional[int] = None

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
    if self.compact_threshold is not None and self.compact_threshold <= 0:
      raise ValueError(
        f'invalid compact_threshold {self.compact_threshold!r}; expected a positive int or None'
      )

  def fast(self) -> Self:
    return dataclasses.replace(self, service_tier='priority')

  def with_effort(self, effort: str) -> Self:
    reasoning_effort = _EFFORT_TO_REASONING_EFFORT.get(effort)
    if reasoning_effort is None:
      raise ValueError(
        f'unknown effort level {effort!r}; expected one of {list(_EFFORT_TO_REASONING_EFFORT)}'
      )
    return dataclasses.replace(self, reasoning_effort=reasoning_effort)

  def needed_secrets(self) -> tuple[str, ...]:
    return ('openai',)

  def create_llm(
    self,
    mcp_servers: Optional[list[MCPServer]] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
  ) -> llm.llm.LLM:
    return ChatGPT.create(
      model=self.model,
      reasoning_effort=self.reasoning_effort,
      service_tier=self.service_tier,
      compact_threshold=self.compact_threshold,
      mcp_servers=mcp_servers,
      observer=observer,
      tracker=tracker,
      agent=agent,
    )

  def dump(self) -> dict:
    return {
      'type': self.TYPE,
      'model': self.model,
      'reasoning_effort': self.reasoning_effort,
      'service_tier': self.service_tier,
      'compact_threshold': self.compact_threshold,
    }

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    # __post_init__ revalidates these against the Literal types; the cast keeps
    # the static checker happy on the JSON-derived path where pyright sees
    # `str | None`.
    return cls(
      model=data['model'],
      reasoning_effort=cast(Optional[ReasoningEffort], data.get('reasoning_effort')),
      service_tier=cast(Optional[ServiceTier], data.get('service_tier')),
      compact_threshold=data.get('compact_threshold'),
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


def _cached_tokens(response_usage: Optional[object]) -> int:
  input_details = (
    getattr(response_usage, 'input_tokens_details', None) if response_usage is not None else None
  )
  return getattr(input_details, 'cached_tokens', 0) if input_details is not None else 0


class ChatGPT(llm.llm.LLM):
  @staticmethod
  def create(
    model: str = 'gpt-5',
    mcp_servers: Optional[list[MCPServer]] = None,
    reasoning_effort: Optional[ReasoningEffort] = None,
    service_tier: Optional[str] = None,
    compact_threshold: Optional[int] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
  ):
    config = credentials.get_json('openai')
    return ChatGPT(
      api_key=config['api_key'],
      model=model,
      mcp_servers=mcp_servers,
      reasoning_effort=reasoning_effort,
      service_tier=service_tier,
      compact_threshold=compact_threshold,
      observer=observer,
      tracker=tracker,
      agent=agent,
    )

  def __init__(
    self,
    api_key: str,
    model: str = 'gpt-5',
    mcp_servers: Optional[list[MCPServer]] = None,
    reasoning_effort: Optional[ReasoningEffort] = None,
    service_tier: Optional[str] = None,
    compact_threshold: Optional[int] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
  ):
    super().__init__(mcp_servers, observer=observer, tracker=tracker, agent=agent)
    self.model = model
    self.client = OpenAI(api_key=api_key)
    self._openai_tools: Optional[list[ToolParam]] = None
    self._last_response_id: Optional[str] = None
    self._reasoning_effort = reasoning_effort
    self._service_tier = service_tier
    self._compact_threshold = compact_threshold
    # cumulative per-model counts in the four billed classes, accumulated from
    # every response's `usage` (see _publish_usage). keyed by the response's
    # resolved model slug (e.g. gpt-5-2025-08-07).
    self._usage_totals: dict[str, usage.Counts] = {}
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
    self._input_prefix: Optional[list[ResponseInputItemParam]] = None

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
      except Exception as exception:
        # surface the failure back to the model as the tool result so the agent
        # can react (retry, switch source, raise) instead of crashing the loop.
        output = f'tool {item.name!r} failed: {type(exception).__name__}: {exception}'
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

  def _context_management_kwargs(self) -> dict:
    # declarative server-side compaction — what bounds context growth in long
    # tool-loop runs. When the chained conversation crosses the threshold, the
    # server compacts it in-band (the response carries a `compaction` output
    # item with the encrypted summary) and `previous_response_id` chaining
    # continues on the compacted state; no client bookkeeping. None sends
    # nothing and leaves growth unbounded.
    if self._compact_threshold is None:
      return {}
    return {
      'context_management': [{'type': 'compaction', 'compact_threshold': self._compact_threshold}]
    }

  def _record_llm_call(self, request: dict, response: Response, *, turn_index: int) -> None:
    # raw request + response payload lands inline in the trail step body. for
    # LocalFileTracker that's a fat JSONL line; HTTPTracker will spill anything
    # over the inline threshold to S3 server-side and replace the body with
    # `{"s3": <key>}` — the bro doesn't know the difference.
    body = {'request': request, 'response': response.model_dump(mode='json')}
    response_usage = getattr(response, 'usage', None)
    tokens_in = getattr(response_usage, 'input_tokens', 0) if response_usage is not None else 0
    tokens_out = getattr(response_usage, 'output_tokens', 0) if response_usage is not None else 0
    output_details = (
      getattr(response_usage, 'output_tokens_details', None) if response_usage is not None else None
    )
    tokens_reasoning = (
      getattr(output_details, 'reasoning_tokens', 0) if output_details is not None else 0
    )
    self.tracker.step(
      'llm_call',
      body,
      turn_index=turn_index,
      response_id=response.id,
      tokens_in=tokens_in,
      tokens_out=tokens_out,
      tokens_reasoning=tokens_reasoning,
      tokens_cached=_cached_tokens(response_usage),
    )

  def _publish_usage(self, response: Response) -> None:
    # fold the response's usage into the instance's cumulative per-model totals
    # and publish the snapshot to the env-pointed usage file, so a commit footer
    # generated by a tool subprocess credits the run's spend. OpenAI reports
    # cached input as a subset of input_tokens: the cached part maps to
    # cache_read, the remainder to input, cache_write stays 0, and reasoning
    # tokens stay inside output.
    response_usage = getattr(response, 'usage', None)
    if response_usage is None:
      return
    input_tokens = getattr(response_usage, 'input_tokens', 0)
    cached = _cached_tokens(response_usage)
    counts = {
      'input': input_tokens - cached,
      'cache_write': 0,
      'cache_read': cached,
      'output': getattr(response_usage, 'output_tokens', 0),
    }
    model = response.model
    self._usage_totals[model] = usage.add(self._usage_totals.get(model, usage.zero()), counts)
    if self.agent is not None:
      usage.publish(self.agent, self._usage_totals)

  def cumulative_usage(self) -> Optional[dict[str, usage.Counts]]:
    return self._usage_totals

  def _build_request_kwargs(
    self,
    input_items: list[ResponseInputItemParam],
    openai_tools: list[ToolParam],
    *,
    previous_response_id: Optional[str],
  ) -> dict:
    kwargs: dict = {
      'model': self.model,
      'input': input_items,
      'tools': openai_tools,
      **self._reasoning_kwargs(),
      **self._service_tier_kwargs(),
      **self._context_management_kwargs(),
    }
    if previous_response_id is not None:
      kwargs['previous_response_id'] = previous_response_id
    return kwargs

  def _create(self, request_kwargs: dict, request_timeout: Optional[float]) -> Response:
    # per-request timeout overrides the client default (the OpenAI SDK's 600s)
    # for this call; None leaves that default in place. responses.create is
    # non-streaming — no bytes return until generation finishes — so without a
    # tighter bound a stalled request blocks the full client timeout before the
    # SDK's automatic retry fires.
    if request_timeout is not None:
      return self.client.responses.create(**request_kwargs, timeout=request_timeout)
    return self.client.responses.create(**request_kwargs)

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
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
    self._publish_usage(response)
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
      self._publish_usage(response)
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
