from typing import get_args

import pytest

from bro.trails.display import (
  ALL_RECORD_KINDS,
  AssistantText,
  DisplayRecord,
  Error,
  HarnessEvent,
  InterimAssistantText,
  LaunchContextEntry,
  LineageNode,
  LLMCall,
  NativeStep,
  Notice,
  Reasoning,
  RecordKind,
  SegmentBoundary,
  SpilledStepBody,
  SystemPrompt,
  ToolCall,
  ToolResult,
  TrailListRow,
  TrailMetadata,
  TransientActivity,
  UserInput,
)


def test_display_record_union_is_closed_over_every_record_kind():
  record_types = get_args(DisplayRecord.__value__)
  assert {record_type.kind for record_type in record_types} == ALL_RECORD_KINDS
  assert set(record_types) == {
    SystemPrompt,
    UserInput,
    Reasoning,
    InterimAssistantText,
    AssistantText,
    LLMCall,
    ToolCall,
    ToolResult,
    Error,
    HarnessEvent,
    TrailMetadata,
    LaunchContextEntry,
    SegmentBoundary,
    NativeStep,
    TrailListRow,
    LineageNode,
    Notice,
    TransientActivity,
  }


def test_record_kind_values_are_stable_human_readable_names():
  assert len(RecordKind) == len(ALL_RECORD_KINDS)
  assert all('_' not in kind.value for kind in RecordKind)


def test_spilled_step_body_validates_the_typed_descriptor():
  descriptor = SpilledStepBody(
    storage_key='trails/steps/T/1.json',
    url='https://example.com/step',
    size=123,
  )
  assert descriptor.size == 123
  with pytest.raises(ValueError, match='non-negative'):
    SpilledStepBody(storage_key='key', url='https://example.com', size=-1)
