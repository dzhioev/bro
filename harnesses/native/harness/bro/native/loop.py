from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Optional

import bro.llm.usage as usage
from bro.base import credentials
from bro.inbox import Inbox, NotificationBatch
from bro.llm import providers, wires
from bro.llm.llm import NativeLLMSpec
from bro.llm.mcp import MCPServer, Tool, ToolControlSignal
from bro.llm.observer import (
  InterimAssistantTextEvent,
  NotificationEvent,
  Observer,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
)
from bro.llm.records import Call, Message, Reasoning
from bro.llm.tracker import ToolStepSource, Tracker
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
from bro.native.llm import LLM


def known_names() -> tuple[str, ...]:
  return tuple(
    name
    for name in providers.known_names()
    if providers.default_spec(name).WIRE in wires.client_names()
  )


def create(
  spec: NativeLLMSpec,
  inbox: Inbox,
  mcp_servers: Optional[list[MCPServer]] = None,
  observer: Optional[Observer] = None,
  tracker: Optional[Tracker] = None,
  agent: Optional[str] = None,
  activity_file: Optional[Path] = None,
) -> LLM:
  secrets = spec.needed_secrets()
  if len(secrets) > 1:
    raise ValueError(f'recipe {spec.TYPE!r} must name one credential material')
  material = credentials.get_json(secrets[0]) if secrets else {}
  return Loop(
    wires.open(spec, material), inbox, mcp_servers, observer, tracker, agent, activity_file
  )


INTERRUPTED_TOOL_OUTPUT = (
  'interrupted by the user before this call returned — it may have partly taken '
  'effect. The user is taking over; do not resume the abandoned work unless they ask.'
)


class Loop(LLM):
  def __init__(
    self,
    wire: Wire,
    inbox: Inbox,
    mcp_servers: Optional[list[MCPServer]] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
    activity_file: Optional[Path] = None,
  ):
    super().__init__(
      inbox,
      mcp_servers,
      observer=observer,
      tracker=tracker,
      agent=agent,
      activity_file=activity_file,
    )
    self.wire = wire
    self._resolved_tools: list[Tool] | None = None
    self._usage_totals: dict[str, usage.Counts] = {}
    self._tool_call_sources: dict[str, ToolStepSource] = {}
    self._call_index = 0
    self._turn_index = 0
    self._has_user_input = False
    self._restored = False
    self._pending_input: list[Input] = []

  def restore(self, prefix: Prefix) -> None:
    self.wire.restore(prefix)
    self._restored = True

  def seed_chain(self, response_id: str) -> None:
    self.wire.seed_chain(response_id)

  async def _resolve_tools(self) -> list[Tool]:
    if self._resolved_tools is None:
      self._resolved_tools = await self.tools.resolve()
    return self._resolved_tools

  def _emit_response_steps(self, leg: Leg, *, llm_call_step_id: int | None) -> None:
    self._tool_call_sources.clear()
    for part in leg.parts:
      if isinstance(part, Reasoning):
        self.observer.on_event(ReasoningEvent(part.text))
      elif isinstance(part, Message) and (leg.calls or part.commentary):
        self.observer.on_event(InterimAssistantTextEvent(part.text))
      elif isinstance(part, Call):
        assert part.call_id is not None and part.name is not None
        self.observer.on_event(ToolCallEvent(part.call_id, part.name, part.arguments))
        if llm_call_step_id is not None:
          self._tool_call_sources[part.call_id] = {'step_id': llm_call_step_id, 'index': part.index}

  @contextlib.contextmanager
  def _current_tool_step(self, call_id: str):
    self.tracker.current_tool_step_id = self._tool_call_sources.get(call_id)
    try:
      yield
    finally:
      self.tracker.current_tool_step_id = None

  async def _execute_tool_calls(self, leg: Leg, *, turn_index: int, call_index: int) -> list[Input]:
    results: list[Input] = []
    calls = leg.calls
    for position, item in enumerate(calls):
      assert item.call_id is not None and item.name is not None
      kwargs = json.loads(item.raw_arguments)
      is_error = False
      try:
        with self._current_tool_step(item.call_id):
          output = await self.tools.call(item.name, kwargs)
      except asyncio.CancelledError:
        results.extend(
          await self._interrupted_outputs(
            calls[position:], turn_index=turn_index, call_index=call_index
          )
        )
        self._pending_input = results
        raise
      except ToolControlSignal:
        raise
      except Exception as exception:
        output = f'tool {item.name!r} failed: {type(exception).__name__}: {exception}'
        is_error = True
      self.observer.on_event(ToolResultEvent(item.call_id, item.name, output, is_error=is_error))
      await self._track_step(
        'tool_result',
        output,
        turn_index=turn_index,
        call_index=call_index,
        tool_name=item.name,
        call_id=item.call_id,
        is_error=is_error,
      )
      results.append(ToolOutput(item.call_id, output))
    return results

  async def _interrupted_outputs(
    self, calls: tuple[Call, ...], *, turn_index: int, call_index: int
  ) -> list[Input]:
    outputs: list[Input] = []
    for item in calls:
      assert item.call_id is not None and item.name is not None
      self.observer.on_event(
        ToolResultEvent(item.call_id, item.name, INTERRUPTED_TOOL_OUTPUT, is_error=True)
      )
      await self._track_step(
        'tool_result',
        INTERRUPTED_TOOL_OUTPUT,
        turn_index=turn_index,
        call_index=call_index,
        tool_name=item.name,
        call_id=item.call_id,
        is_error=True,
      )
      outputs.append(ToolOutput(item.call_id, INTERRUPTED_TOOL_OUTPUT))
    return outputs

  def _publish_usage(self, leg: Leg) -> None:
    if leg.counts is None:
      return
    self._usage_totals[leg.model] = usage.add(
      self._usage_totals.get(leg.model, usage.zero()), leg.counts
    )
    if self.agent is not None:
      usage.publish(self.agent, self._usage_totals)

  def cumulative_usage(self) -> Optional[dict[str, usage.Counts]]:
    return self._usage_totals

  async def _exchange(
    self, inputs: list[Input], tools: list[Tool], *, request_timeout: float | None
  ) -> Leg:
    self._call_index += 1
    try:
      leg = await self.wire.exchange(inputs, tools, request_timeout)
    except asyncio.CancelledError:
      self._pending_input = inputs
      raise
    except UnusableResponse as error:
      await self._record_leg(error.leg)
      self._pending_input = []
      raise
    await self._record_leg(leg)
    return leg

  async def _record_leg(self, leg: Leg) -> None:
    step_id = await self._track_step(
      'llm_call',
      leg.record,
      turn_index=self._turn_index,
      call_index=self._call_index,
      wire=leg.wire,
      **leg.extras,
    )
    self._publish_usage(leg)
    self._emit_response_steps(leg, llm_call_step_id=step_id)

  async def _drain_notifications(
    self,
    input_items: list[Input],
    *,
    call_index: int,
    following: Optional[list[Input]] = None,
  ) -> Optional[NotificationBatch]:
    batch = self.inbox.drain()
    if batch is None:
      return None
    input_items.append(Notification(batch.text))
    if following is not None:
      input_items.extend(following)
    # The drain consumed the jobs' news. Pin every item in its request before
    # the first await so cancellation while recording cannot lose any of them.
    self._pending_input = list(input_items)
    await self._track_step(
      'notification',
      batch.text,
      turn_index=self._turn_index,
      call_index=call_index,
      job_ids=list(batch.job_ids),
    )
    self.observer.on_event(NotificationEvent(batch.text, batch.job_ids))
    return batch

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    return await self._send(messages, notification_turn=False, request_timeout=request_timeout)

  async def wake(self, *, request_timeout: Optional[float] = None) -> str:
    return await self._send([], notification_turn=True, request_timeout=request_timeout)

  async def _send(
    self,
    messages: list[dict],
    *,
    notification_turn: bool,
    request_timeout: Optional[float],
  ) -> str:
    tools = await self._resolve_tools()
    api_input: list[Input] = []
    incoming = messages
    if self._restored:
      incoming = [message for message in messages if message.get('role') != 'system']
      self._restored = False
    api_input.extend(self._pending_input)
    self._pending_input = []

    user_messages = [message for message in incoming if message.get('role') == 'user']
    if notification_turn or (len(user_messages) > 0 and self._has_user_input):
      self._turn_index += 1
    converted_incoming: list[Input] = [_convert_message(message) for message in incoming]
    notification = await self._drain_notifications(
      api_input,
      call_index=self._call_index + 1,
      following=converted_incoming,
    )
    if notification_turn and notification is None:
      raise RuntimeError('notification turn started without inbox news')
    if notification is None:
      api_input.extend(converted_incoming)

    for message in user_messages:
      await self._track_step('user_input', _extract_text(message), turn_index=self._turn_index)
    if notification_turn or len(user_messages) > 0:
      self._has_user_input = True

    response = await self._exchange(api_input, tools, request_timeout=request_timeout)
    self._pending_input = []
    while response.calls:
      tool_results = await self._execute_tool_calls(
        response,
        turn_index=self._turn_index,
        call_index=self._call_index,
      )
      await self._drain_notifications(tool_results, call_index=self._call_index)
      response = await self._exchange(tool_results, tools, request_timeout=request_timeout)
      self._pending_input = []

    assert response.reply is not None
    return response.reply


def _convert_message(message: dict) -> Input:
  if message['role'] == 'system':
    return SystemPrompt(message['content'])
  if message['role'] != 'user':
    raise ValueError(f'unsupported incoming role {message["role"]!r}')
  return UserMessage(message['content'])


def _extract_text(msg: dict) -> str:
  content = msg.get('content', '')
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    return '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
  return str(content)
