from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Optional

import bro.llm.usage as usage
from bro.base import credentials
from bro.inbox import Inbox, NotificationBatch
from bro.llm import providers, wires
from bro.llm.llm import LLMSpec, LLMStopped
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
from bro.llm.retry import retry_llm
from bro.llm.tracker import ToolStepSource, Tracker
from bro.llm.wire import (
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
from bro.native.llm import LLM


def known_names() -> tuple[str, ...]:
  return tuple(
    name for name, spec in providers.available_specs() if spec.WIRE in wires.client_names()
  )


def create(
  spec: LLMSpec,
  inbox: Inbox,
  mcp_servers: Optional[list[MCPServer]] = None,
  observer: Optional[Observer] = None,
  tracker: Optional[Tracker] = None,
  agent: Optional[str] = None,
  activity_file: Optional[Path] = None,
) -> LLM:
  if spec.WIRE not in wires.client_names():
    raise ValueError(f'native harness cannot run wire {spec.WIRE!r}: no installed client')
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
    try:
      for item in calls:
        assert item.call_id is not None and item.name is not None
        is_error = False
        try:
          with self._current_tool_step(item.call_id):
            if item.arguments_are_object:
              output = await self.tools.call(item.name, item.arguments)
            else:
              output = f'tool {item.name!r} failed: arguments are not a JSON object'
              is_error = True
        except ToolControlSignal:
          raise
        except Exception as exception:
          output = f'tool {item.name!r} failed: {type(exception).__name__}: {exception}'
          is_error = True
        results.append(ToolOutput(item.call_id, output))
        await _finish_on_cancel(
          self._record_tool_result(
            item, output, is_error=is_error, turn_index=turn_index, call_index=call_index
          )
        )
    except asyncio.CancelledError:
      await _finish_on_cancel(
        self._park_interrupted(
          calls[len(results) :], results, turn_index=turn_index, call_index=call_index
        )
      )
      raise
    return results

  async def _park_interrupted(
    self, calls: tuple[Call, ...], outputs: list[Input], *, turn_index: int, call_index: int
  ) -> None:
    self._pending_input = outputs
    outputs.extend(
      await self._interrupted_outputs(calls, turn_index=turn_index, call_index=call_index)
    )

  async def _interrupted_outputs(
    self, calls: tuple[Call, ...], *, turn_index: int, call_index: int
  ) -> list[Input]:
    outputs: list[Input] = []
    for item in calls:
      assert item.call_id is not None and item.name is not None
      outputs.append(ToolOutput(item.call_id, INTERRUPTED_TOOL_OUTPUT))
      await _finish_on_cancel(
        self._record_tool_result(
          item, INTERRUPTED_TOOL_OUTPUT, is_error=True, turn_index=turn_index, call_index=call_index
        )
      )
    return outputs

  async def _record_tool_result(
    self, item: Call, output: dict | str, *, is_error: bool, turn_index: int, call_index: int
  ) -> None:
    assert item.call_id is not None and item.name is not None
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
    self._pending_input = inputs
    try:
      leg = await retry_llm(
        lambda: self.wire.exchange(inputs, tools, request_timeout),
        lambda error: False,
        capacity=lambda leg: leg.stop == 'capacity',
        on_capacity=self._record_capacity_leg,
      )
    except asyncio.CancelledError:
      self._pending_input = inputs
      raise
    except UnusableResponse as error:
      self._pending_input = [] if error.accepted else inputs
      await _finish_on_cancel(self._record_leg(error.leg, rejected=not error.accepted))
      raise
    if leg.stop == 'completed':
      self._pending_input = []
    try:
      await _finish_on_cancel(self._record_leg(leg))
    except asyncio.CancelledError:
      if leg.stop == 'completed':
        await _finish_on_cancel(
          self._park_interrupted(
            leg.calls, [], turn_index=self._turn_index, call_index=self._call_index
          )
        )
      raise
    if leg.stop != 'completed':
      raise LLMStopped(leg.stop, leg.stop_reason)
    return leg

  async def _record_capacity_leg(self, leg: Leg) -> None:
    await _finish_on_cancel(self._record_leg(leg))

  async def _record_leg(self, leg: Leg, *, rejected: bool = False) -> None:
    stopped = 'failed' if rejected else leg.stop
    step_id = await self._track_step(
      'llm_call',
      leg.record,
      turn_index=self._turn_index,
      call_index=self._call_index,
      wire=leg.wire,
      **leg.extras,
      **({'stopped': stopped} if stopped != 'completed' else {}),
    )
    self._publish_usage(leg)
    if stopped == 'completed':
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

  async def send(
    self, inputs: Sequence[SystemPrompt | UserMessage], *, request_timeout: Optional[float] = None
  ) -> str:
    return await self._send(inputs, notification_turn=False, request_timeout=request_timeout)

  async def wake(self, *, request_timeout: Optional[float] = None) -> str:
    return await self._send([], notification_turn=True, request_timeout=request_timeout)

  async def _send(
    self,
    inputs: Sequence[SystemPrompt | UserMessage],
    *,
    notification_turn: bool,
    request_timeout: Optional[float],
  ) -> str:
    tools = await self._resolve_tools()
    api_input: list[Input] = []
    incoming = inputs
    if self._restored:
      incoming = [item for item in inputs if not isinstance(item, SystemPrompt)]
      self._restored = False
    api_input.extend(self._pending_input)

    user_messages = [item for item in incoming if isinstance(item, UserMessage)]
    if notification_turn or (len(user_messages) > 0 and self._has_user_input):
      self._turn_index += 1
    incoming_items: list[Input] = list(incoming)
    notification = await self._drain_notifications(
      api_input,
      call_index=self._call_index + 1,
      following=incoming_items,
    )
    if notification_turn and notification is None:
      raise RuntimeError('notification turn started without inbox news')
    if notification is None:
      api_input.extend(incoming_items)

    self._pending_input = list(api_input)
    for message in user_messages:
      await _finish_on_cancel(
        self._track_step(
          'user_input',
          '\n'.join(item.text for item in message.content if isinstance(item, Text)),
          turn_index=self._turn_index,
        )
      )
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
      self._pending_input = tool_results
      await self._drain_notifications(tool_results, call_index=self._call_index)
      response = await self._exchange(tool_results, tools, request_timeout=request_timeout)
      self._pending_input = []

    assert response.reply is not None
    return response.reply


async def _finish_on_cancel[T](operation: Awaitable[T]) -> T:
  # Persist an accepted leg or result before its interruption records can follow it.
  task = asyncio.ensure_future(operation)
  cancelled = False
  while not task.done():
    try:
      await asyncio.shield(task)
    except asyncio.CancelledError:
      cancelled = True
  result = task.result()
  if cancelled:
    raise asyncio.CancelledError
  return result
