from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Optional

import bro.llm.usage as usage
from bro.base import credentials, log
from bro.llm.llm import NativeLLMSpec
from bro.llm.llms.openai import (
  DEFAULT_MODEL as _DEFAULT_MODEL,
  LLMSpec as _OpenAISpec,
  ReasoningEffort as _ReasoningEffort,
)
from bro.llm.mcp import MCPServer, Tool, ToolControlSignal
from bro.llm.observer import (
  InterimAssistantTextEvent,
  Observer,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
)
from bro.llm.openai_content import image_file_to_content, text_to_content
from bro.llm.tracker import ToolStepSource, Tracker
from bro.native.llm import LLM

if TYPE_CHECKING:
  from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseInputItemParam,
    ResponseOutputItem,
    ResponseOutputMessage,
    ToolParam,
  )
  from openai.types.responses.easy_input_message_param import EasyInputMessageParam
  from openai.types.responses.response_input_content_param import ResponseInputContentParam


def create(
  spec: NativeLLMSpec,
  mcp_servers: Optional[list[MCPServer]] = None,
  observer: Optional[Observer] = None,
  tracker: Optional[Tracker] = None,
  agent: Optional[str] = None,
) -> LLM:
  if not isinstance(spec, _OpenAISpec):
    raise TypeError(
      f'expected {_OpenAISpec.__module__}.LLMSpec, got {type(spec).__module__}.{type(spec).__name__}'
    )
  config = credentials.get_json('openai')
  return OpenAI(
    api_key=config['api_key'],
    model=spec.model,
    reasoning_effort=spec.reasoning_effort,
    service_tier=spec.service_tier,
    compact_threshold=spec.compact_threshold,
    mcp_servers=mcp_servers,
    observer=observer,
    tracker=tracker,
    agent=agent,
  )


def extract_reply_messages(output: list[ResponseOutputItem]) -> list[ResponseOutputMessage]:
  messages = [item for item in output if item.type == 'message']
  if len(messages) == 0:
    raise RuntimeError(f"output doesn't contain output messages. output: {output}")
  # gpt-5.6 models can emit auxiliary messages tagged with a `phase` field (a
  # 'commentary' progress note preceding the 'final_answer' reply); the field
  # is not in the SDK's ResponseOutputMessage, hence the getattr.
  final_answers = [m for m in messages if getattr(m, 'phase', None) == 'final_answer']
  return final_answers if len(final_answers) > 0 else messages


def _message_texts(messages: list[ResponseOutputMessage]) -> list[str]:
  texts = []
  for message in messages:
    chunks = [item.text for item in message.content if item.type == 'output_text']
    if len(chunks) > 0:
      texts.append(''.join(chunks))
  return texts


def parse_response(response: Response) -> str:
  messages = extract_reply_messages(response.output)
  for message in messages:
    for item in message.content:
      if item.type == 'refusal':
        raise RuntimeError(f'got refusal: {item.refusal}')
      assert item.type == 'output_text'
  texts = _message_texts(messages)
  if len(texts) == 0:
    raise RuntimeError(f'no output texts in messages: {messages}')
  return '\n\n'.join(texts)


def has_tool_calls(response: Response) -> bool:
  return any(item.type == 'function_call' for item in response.output)


def tools_to_openai_format(tools: list[Tool]) -> list[ToolParam]:
  # strict mode is intentionally off: it forces every property into `required`,
  # which makes pydantic-`default=None` fields un-omittable and traps the model
  # into clobbering them with default-looking values on every call. With
  # strict=False the schema flows through as pydantic generated it and optional
  # fields stay genuinely optional.
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
  return text_to_content(str(part))


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


# the result a tool call gets when the turn is interrupted before it returned;
# also what the trail records for it. read by the model on the next turn.
INTERRUPTED_TOOL_OUTPUT = (
  'interrupted by the user before this call returned — it may have partly taken '
  'effect. The user is taking over; do not resume the abandoned work unless they ask.'
)


class OpenAI(LLM):
  def __init__(
    self,
    api_key: str,
    model: str = _DEFAULT_MODEL,
    mcp_servers: Optional[list[MCPServer]] = None,
    reasoning_effort: Optional[_ReasoningEffort] = None,
    service_tier: Optional[str] = None,
    compact_threshold: Optional[int] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
  ):
    from openai import AsyncOpenAI

    super().__init__(mcp_servers, observer=observer, tracker=tracker, agent=agent)
    self.model = model
    # async is what makes a roundtrip interruptible: a cancelled await closes
    # the request, where the sync client pins the loop until the reply lands.
    self.client = AsyncOpenAI(api_key=api_key)
    self._openai_tools: Optional[list[ToolParam]] = None
    self._last_response_id: Optional[str] = None
    self._reasoning_effort = reasoning_effort
    self._service_tier = service_tier
    self._compact_threshold = compact_threshold
    # cumulative per-model counts in the four billed classes, accumulated from
    # every response's `usage` (see _publish_usage). keyed by the response's
    # resolved model slug (e.g. gpt-5-2025-08-07).
    self._usage_totals: dict[str, usage.Counts] = {}
    self._tool_call_sources: dict[str, ToolStepSource] = {}
    self._call_index = 0
    self._turn_index = 0
    self._has_user_input = False
    # client-side fork seam: bro.fork sets this to the replayed conversation
    # prefix (already in OpenAI input shape — system message at index 0, user
    # messages, model output items, function_call_outputs). consumed on the
    # first send: prepended to the API input and a system message in the
    # incoming messages list is dropped (the prefix already carries the
    # system). cleared after one use so subsequent send()s behave normally.
    self._input_prefix: Optional[list[ResponseInputItemParam]] = None
    # what an interrupted turn left unacknowledged: the request items no
    # response came back for, or the tool outputs the loop never got to send.
    # they lead the next send's input, on the chain `_last_response_id` names.
    self._pending_input: list[ResponseInputItemParam] = []

  async def _resolve_openai_tools(self) -> list[ToolParam]:
    if self._openai_tools is not None:
      return self._openai_tools
    tools = await self.tools.resolve()
    self._openai_tools = tools_to_openai_format(tools)
    return self._openai_tools

  def _emit_response_steps(
    self,
    response: Response,
    *,
    llm_call_step_id: Optional[int],
  ) -> None:
    self._tool_call_sources.clear()
    continues = has_tool_calls(response)
    for index, item in enumerate(response.output, start=1):
      if item.type == 'reasoning':
        for part in item.summary:
          if part.type == 'summary_text' and len(part.text) > 0:
            self.observer.on_event(ReasoningEvent(part.text))
      elif item.type == 'message' and continues:
        text = ''.join(content.text for content in item.content if content.type == 'output_text')
        if len(text) > 0:
          self.observer.on_event(InterimAssistantTextEvent(text))
      elif item.type == 'function_call':
        try:
          arguments = json.loads(item.arguments)
        except json.JSONDecodeError:
          arguments = {'_raw_arguments': item.arguments}
        self.observer.on_event(ToolCallEvent(item.call_id, item.name, arguments))
        if llm_call_step_id is not None:
          self._tool_call_sources[item.call_id] = {
            'step_id': llm_call_step_id,
            'index': index,
          }

  @contextlib.contextmanager
  def _current_tool_step(self, call_id: str):
    self.tracker.current_tool_step_id = self._tool_call_sources.get(call_id)
    try:
      yield
    finally:
      self.tracker.current_tool_step_id = None

  async def _execute_tool_calls(
    self, response: Response, *, turn_index: int, call_index: int
  ) -> list[ResponseInputItemParam]:
    results: list[ResponseInputItemParam] = []
    calls = [item for item in response.output if item.type == 'function_call']
    for position, item in enumerate(calls):
      kwargs = json.loads(item.arguments)
      is_error = False
      try:
        with self._current_tool_step(item.call_id):
          output = await self.tools.call(item.name, kwargs)
      except asyncio.CancelledError:
        results.extend(
          self._interrupted_outputs(calls[position:], turn_index=turn_index, call_index=call_index)
        )
        self._pending_input = results
        raise
      except ToolControlSignal:
        raise
      except Exception as exception:
        # surface the failure back to the model as the tool result so the agent
        # can react (retry, switch source, raise) instead of crashing the loop.
        output = f'tool {item.name!r} failed: {type(exception).__name__}: {exception}'
        is_error = True
      self.observer.on_event(ToolResultEvent(item.call_id, item.name, output, is_error=is_error))
      # tracker body keeps the raw tool output (dict or str) — the JSON encoding
      # we do below for the API is a wire-format concern only.
      self.tracker.step(
        'tool_result',
        output,
        turn_index=turn_index,
        call_index=call_index,
        tool_name=item.name,
        call_id=item.call_id,
        is_error=is_error,
      )
      if isinstance(output, dict):
        output = json.dumps(output)
      results.append({'type': 'function_call_output', 'call_id': item.call_id, 'output': output})
    return results

  def _interrupted_outputs(
    self, calls: list[ResponseFunctionToolCall], *, turn_index: int, call_index: int
  ) -> list[ResponseInputItemParam]:
    # every call the interruption left unanswered still needs a result: the API
    # rejects a turn whose function calls have no output, and the model has to
    # learn its work was stopped rather than silently lose it.
    outputs: list[ResponseInputItemParam] = []
    for item in calls:
      self.observer.on_event(
        ToolResultEvent(item.call_id, item.name, INTERRUPTED_TOOL_OUTPUT, is_error=True)
      )
      self.tracker.step(
        'tool_result',
        INTERRUPTED_TOOL_OUTPUT,
        turn_index=turn_index,
        call_index=call_index,
        tool_name=item.name,
        call_id=item.call_id,
        is_error=True,
      )
      outputs.append(
        {
          'type': 'function_call_output',
          'call_id': item.call_id,
          'output': INTERRUPTED_TOOL_OUTPUT,
        }
      )
    return outputs

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

  def _record_llm_call(
    self,
    request: dict,
    response: Response,
    *,
    turn_index: int,
    call_index: int,
  ) -> Optional[int]:
    body = {'request': request, 'response': response.model_dump(mode='json')}
    return self.tracker.step(
      'llm_call',
      body,
      turn_index=turn_index,
      call_index=call_index,
      response_id=response.id,
    )

  def _publish_usage(self, response: Response) -> None:
    # fold the response's usage into the instance's cumulative per-model totals
    # and publish the snapshot to the env-pointed usage file, so a commit footer
    # generated by a tool subprocess credits the run's spend.
    response_usage = response.usage
    if response_usage is None:
      return
    details = response_usage.input_tokens_details
    counts = usage.from_vendor_counts(
      {
        'input_tokens': response_usage.input_tokens,
        'input_tokens_details': {
          'cached_tokens': details.cached_tokens,
          'cache_write_tokens': details.cache_write_tokens,
        },
        'output_tokens': response_usage.output_tokens,
      }
    )
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

  async def _create(self, request_kwargs: dict, request_timeout: Optional[float]) -> Response:
    # per-request timeout overrides the client default (the OpenAI SDK's 600s)
    # for this call; None leaves that default in place. responses.create is
    # non-streaming — no bytes return until generation finishes — so without a
    # tighter bound a stalled request blocks the full client timeout before the
    # SDK's automatic retry fires.
    if request_timeout is not None:
      return await self.client.responses.create(**request_kwargs, timeout=request_timeout)
    return await self.client.responses.create(**request_kwargs)

  async def _exchange(
    self,
    input_items: list[ResponseInputItemParam],
    openai_tools: list[ToolParam],
    *,
    request_timeout: Optional[float],
  ) -> Response:
    # one request/response leg of the tool loop, chained onto the last response
    # actually received — which is also what makes an interruption recoverable:
    # the chain pointer never names a response whose items we abandoned.
    request_kwargs = self._build_request_kwargs(
      input_items, openai_tools, previous_response_id=self._last_response_id
    )
    self._call_index += 1
    try:
      response = await self._create(request_kwargs, request_timeout)
    except asyncio.CancelledError:
      self._pending_input = input_items
      raise
    self._last_response_id = response.id
    llm_call_step_id = self._record_llm_call(
      request_kwargs,
      response,
      turn_index=self._turn_index,
      call_index=self._call_index,
    )
    self._publish_usage(response)
    self._emit_response_steps(response, llm_call_step_id=llm_call_step_id)
    return response

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
    api_input.extend(self._pending_input)
    self._pending_input = []
    api_input.extend(convert_message(msg) for msg in incoming)

    user_messages = [message for message in incoming if message.get('role') == 'user']
    for message in user_messages:
      if self._has_user_input:
        self._turn_index += 1
      self.tracker.step('user_input', _extract_text(message), turn_index=self._turn_index)
      self._has_user_input = True

    response = await self._exchange(api_input, openai_tools, request_timeout=request_timeout)
    while has_tool_calls(response):
      tool_results = await self._execute_tool_calls(
        response,
        turn_index=self._turn_index,
        call_index=self._call_index,
      )
      response = await self._exchange(tool_results, openai_tools, request_timeout=request_timeout)

    try:
      return parse_response(response)
    except Exception as error:
      # by now every tool call has executed and the terminal response is
      # recorded, so an extraction failure degrades to the plain terminal
      # message text instead of failing a run whose work is complete; with no
      # text at all there is no reply to salvage and the failure propagates.
      texts = _message_texts([item for item in response.output if item.type == 'message'])
      if len(texts) == 0:
        raise
      log.warning('reply extraction failed, falling back to the terminal message text: %s', error)
      return '\n\n'.join(texts)


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
