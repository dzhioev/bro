from dataclasses import fields

import pytest

from bro.llm.observer import (
  InterimAssistantTextEvent,
  NullObserver,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)


def test_null_observer_discards_every_event():
  observer = NullObserver()
  events = [
    TurnStartedEvent('input'),
    ReasoningEvent('thinking'),
    InterimAssistantTextEvent('working'),
    ToolCallEvent('call-1', 'service__tool', {'value': 1}),
    ToolResultEvent('call-1', 'service__tool', {'ok': True}),
    TurnCompletedEvent('done'),
    TurnRefusedEvent('no'),
    TurnFailedEvent('failed'),
  ]

  for event in events:
    assert observer.on_event(event) is None


def test_tool_events_carry_correlation_and_error_state():
  call = ToolCallEvent('call-1', 'service__tool', {'value': 1})
  result = ToolResultEvent('call-1', 'service__tool', 'failed', is_error=True)

  assert call.call_id == result.call_id
  assert result.is_error is True
  assert [field.name for field in fields(ToolResultEvent)] == [
    'call_id',
    'tool_name',
    'result',
    'is_error',
  ]


def test_tool_events_reject_empty_identity():
  with pytest.raises(ValueError, match='identity and name'):
    ToolCallEvent('', 'service__tool', {})
  with pytest.raises(ValueError, match='identity and name'):
    ToolCallEvent('call-1', '', {})
  with pytest.raises(ValueError, match='identity and name'):
    ToolResultEvent('', 'service__tool', 'result')
  with pytest.raises(ValueError, match='identity and name'):
    ToolResultEvent('call-1', '', 'result')
