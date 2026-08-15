from datetime import UTC, datetime
from typing import cast

from bro.llm.observer import (
  InterimAssistantTextEvent,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.trails.display.core import DisplaySession
from bro.trails.display.live import LiveDisplayObserver
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


class _CapturingSession:
  def __init__(self):
    self.records = []
    self.entered = False
    self.exited = False

  def __enter__(self):
    self.entered = True
    return self

  def __exit__(self, exception_type, exception, traceback):
    self.exited = True

  def consume(self, record):
    self.records.append(record)


def test_live_adapter_maps_all_events_with_arrival_provenance():
  session = _CapturingSession()
  observer = LiveDisplayObserver(
    cast(DisplaySession, session),
    run_id='run-1',
    now=lambda: datetime(2026, 8, 15, 1, 2, 3, tzinfo=UTC),
  )
  events = [
    TurnStartedEvent('input'),
    ReasoningEvent('thinking'),
    InterimAssistantTextEvent('working'),
    ToolCallEvent('call-1', 'service__tool', {'value': 1}),
    ToolResultEvent('call-1', 'service__tool', {'ok': True}, is_error=True),
    TurnCompletedEvent('done'),
    TurnRefusedEvent('refused'),
    TurnFailedEvent('failed'),
  ]

  for event in events:
    observer.on_event(event)

  assert [type(record) for record in session.records] == [
    UserInput,
    Reasoning,
    InterimAssistantText,
    ToolCall,
    ToolResult,
    AssistantText,
    AssistantText,
    Error,
  ]
  for sequence, record in enumerate(session.records):
    assert record.key == f'live:run-1:{sequence}'
    assert record.origin is Origin.LIVE
    assert record.source == LiveSource('run-1', sequence)
    assert record.timestamp == '2026-08-15T01:02:03+00:00'
  result = session.records[4]
  assert isinstance(result, ToolResult)
  assert result.call_id == 'call-1'
  assert result.tool_name == 'service__tool'
  assert result.is_error is True


def test_live_adapter_emits_keyed_conversation_activity():
  session = _CapturingSession()
  observer = LiveDisplayObserver(
    cast(DisplaySession, session),
    run_id='run-1',
    activity_id='turn',
  )

  observer.on_event(TurnStartedEvent('input'))
  observer.on_event(ToolCallEvent('call-1', 'service__first', {}))
  observer.on_event(ToolCallEvent('call-2', 'service__second', {}))
  observer.on_event(ToolResultEvent('call-1', 'service__first', 'one'))
  observer.on_event(ToolResultEvent('call-2', 'service__second', 'two'))
  observer.on_event(TurnCompletedEvent('done'))

  activities = [record for record in session.records if isinstance(record, TransientActivity)]
  assert [(record.content, record.active) for record in activities] == [
    ('thinking', True),
    ('calling service::first', True),
    ('calling 2 tools', True),
    ('calling service::second', True),
    ('thinking', True),
    ('', False),
  ]
  assert {record.activity_id for record in activities} == {'turn'}
  assert observer.turn_finished


def test_live_adapter_owns_the_display_session_context():
  session = _CapturingSession()
  observer = LiveDisplayObserver(cast(DisplaySession, session), run_id='run-1')

  with observer:
    assert session.entered is True

  assert session.exited is True
