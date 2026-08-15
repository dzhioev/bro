"""Adapt provider-neutral live events into semantic display records."""

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self, assert_never
from uuid import uuid4

from bro.llm.observer import (
  InterimAssistantTextEvent,
  ObservedEvent,
  Observer,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.trails.display.core import DisplaySession
from bro.trails.display.records import (
  AssistantText,
  Error,
  InterimAssistantText,
  LiveSource,
  Origin,
  Reasoning,
  ToolCall,
  ToolResult,
  TransientActivity,
  UserInput,
)


def _arrival_time() -> datetime:
  return datetime.now().astimezone()


class LiveDisplayObserver(Observer):
  """Stamp live events with run-local provenance and feed a display session."""

  def __init__(
    self,
    session: DisplaySession,
    *,
    run_id: str | None = None,
    now: Callable[[], datetime] = _arrival_time,
    activity_id: str | None = None,
  ):
    self.session = session
    self.run_id = run_id if run_id is not None else str(uuid4())
    if len(self.run_id) == 0:
      raise ValueError('live display run_id must not be empty')
    self._now = now
    self._sequence = 0
    self._activity_id = activity_id
    self._activity_sequence = 0
    self._pending_tools: dict[str, str] = {}
    self.turn_finished = False

  def __enter__(self) -> Self:
    self.session.__enter__()
    return self

  def __exit__(
    self,
    exception_type: type[BaseException] | None,
    exception: BaseException | None,
    traceback: TracebackType | None,
  ) -> None:
    self.session.__exit__(exception_type, exception, traceback)

  def on_event(self, event: ObservedEvent) -> None:
    sequence = self._sequence
    self._sequence += 1
    source = LiveSource(run_id=self.run_id, sequence=sequence)
    common = {
      'key': f'live:{self.run_id}:{sequence}',
      'origin': Origin.LIVE,
      'source': source,
      'timestamp': self._now().isoformat(),
    }
    if isinstance(event, TurnStartedEvent):
      record = UserInput(content=event.input, **common)
    elif isinstance(event, ReasoningEvent):
      record = Reasoning(content=event.content, **common)
    elif isinstance(event, InterimAssistantTextEvent):
      record = InterimAssistantText(content=event.content, **common)
    elif isinstance(event, ToolCallEvent):
      record = ToolCall(
        call_id=event.call_id,
        tool_name=event.tool_name,
        arguments=event.arguments,
        **common,
      )
    elif isinstance(event, ToolResultEvent):
      record = ToolResult(
        call_id=event.call_id,
        tool_name=event.tool_name,
        result=event.result,
        is_error=event.is_error,
        **common,
      )
    elif isinstance(event, (TurnCompletedEvent, TurnRefusedEvent)):
      content = event.content if isinstance(event, TurnCompletedEvent) else event.reason
      record = AssistantText(content=content, **common)
    elif isinstance(event, TurnFailedEvent):
      record = Error(content=event.error, **common)
    else:
      assert_never(event)
    self.session.consume(record)
    if isinstance(event, TurnStartedEvent):
      self.turn_finished = False
    elif isinstance(event, (TurnCompletedEvent, TurnRefusedEvent, TurnFailedEvent)):
      self.turn_finished = True
    if self._activity_id is not None:
      self._update_activity(event)

  def close_activity(self) -> None:
    if self._activity_id is None:
      return
    self._pending_tools.clear()
    self._consume_activity('', active=False)

  def _update_activity(self, event: ObservedEvent) -> None:
    if isinstance(event, TurnStartedEvent):
      if len(self._pending_tools) > 0:
        raise RuntimeError('a new turn started while tool activity was still pending')
      self._consume_activity('thinking')
    elif isinstance(event, ToolCallEvent):
      self._pending_tools[event.call_id] = event.tool_name
      self._consume_activity(self._activity_content())
    elif isinstance(event, ToolResultEvent):
      self._pending_tools.pop(event.call_id, None)
      self._consume_activity(self._activity_content())
    elif isinstance(event, (TurnCompletedEvent, TurnRefusedEvent, TurnFailedEvent)):
      self.close_activity()
    elif isinstance(event, (ReasoningEvent, InterimAssistantTextEvent)):
      return
    else:
      assert_never(event)

  def _activity_content(self) -> str:
    if len(self._pending_tools) == 0:
      return 'thinking'
    if len(self._pending_tools) == 1:
      from bro.llm.mcp import canonical_name

      return f'calling {canonical_name(next(iter(self._pending_tools.values())))}'
    return f'calling {len(self._pending_tools)} tools'

  def _consume_activity(self, content: str, *, active: bool = True) -> None:
    assert self._activity_id is not None
    sequence = self._activity_sequence
    self._activity_sequence += 1
    self.session.consume(
      TransientActivity(
        key=f'surface:{self.run_id}:activity:{sequence}',
        origin=Origin.SURFACE,
        activity_id=self._activity_id,
        content=content,
        active=active,
      )
    )
