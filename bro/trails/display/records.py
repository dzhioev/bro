"""Typed semantic records accepted by the trails display core."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar


class Origin(StrEnum):
  RECORDED = 'recorded'
  LIVE = 'live'
  SURFACE = 'surface'


class RecordKind(StrEnum):
  SYSTEM_PROMPT = 'system-prompt'
  USER_INPUT = 'user-input'
  REASONING = 'reasoning'
  INTERIM_ASSISTANT = 'interim-assistant'
  ASSISTANT = 'assistant'
  LLM_CALL = 'llm-call'
  TOOL_CALL = 'tool-call'
  TOOL_RESULT = 'tool-result'
  ERROR = 'error'
  HARNESS_EVENT = 'harness-event'
  TRAIL_METADATA = 'trail-metadata'
  LAUNCH_CONTEXT = 'launch-context'
  SEGMENT_BOUNDARY = 'segment-boundary'
  NATIVE_STEP = 'native-step'
  TRAIL_LIST_ROW = 'trail-list-row'
  LINEAGE_NODE = 'lineage-node'
  NOTICE = 'notice'
  TRANSIENT_ACTIVITY = 'transient-activity'


@dataclass(frozen=True)
class RecordedSource:
  trail_id: str
  step_id: int
  index: int = 0

  def __post_init__(self) -> None:
    if len(self.trail_id) == 0:
      raise ValueError('recorded source trail_id must not be empty')
    if self.step_id < 0 or self.index < 0:
      raise ValueError('recorded source positions must be non-negative')


@dataclass(frozen=True)
class LiveSource:
  run_id: str
  sequence: int

  def __post_init__(self) -> None:
    if len(self.run_id) == 0:
      raise ValueError('live source run_id must not be empty')
    if self.sequence < 0:
      raise ValueError('live source sequence must be non-negative')


type RecordSource = RecordedSource | LiveSource


@dataclass(frozen=True, kw_only=True)
class Record:
  """Common identity and provenance carried by every display record."""

  kind: ClassVar[RecordKind]
  key: str
  origin: Origin
  source: RecordSource | None = None
  timestamp: str | None = None

  def __post_init__(self) -> None:
    if len(self.key) == 0:
      raise ValueError('record key must not be empty')
    if self.timestamp == '':
      raise ValueError('record timestamp must be non-empty when present')
    if self.origin is Origin.RECORDED and self.source is not None:
      if not isinstance(self.source, RecordedSource):
        raise ValueError('recorded record sources must be RecordedSource values')
    if self.origin is Origin.LIVE and self.source is not None:
      if not isinstance(self.source, LiveSource):
        raise ValueError('live record sources must be LiveSource values')
    if self.origin is Origin.SURFACE and self.source is not None:
      raise ValueError('surface records cannot claim recorded or live provenance')


@dataclass(frozen=True, kw_only=True)
class SystemPrompt(Record):
  kind: ClassVar[RecordKind] = RecordKind.SYSTEM_PROMPT
  content: str


@dataclass(frozen=True, kw_only=True)
class UserInput(Record):
  kind: ClassVar[RecordKind] = RecordKind.USER_INPUT
  content: str
  is_meta: bool = False
  is_sidechain: bool = False


@dataclass(frozen=True, kw_only=True)
class Reasoning(Record):
  kind: ClassVar[RecordKind] = RecordKind.REASONING
  content: str


@dataclass(frozen=True, kw_only=True)
class InterimAssistantText(Record):
  kind: ClassVar[RecordKind] = RecordKind.INTERIM_ASSISTANT
  content: str
  markdown: bool = True


@dataclass(frozen=True, kw_only=True)
class AssistantText(Record):
  kind: ClassVar[RecordKind] = RecordKind.ASSISTANT
  content: str
  markdown: bool = True


@dataclass(frozen=True, kw_only=True)
class LLMCall(Record):
  kind: ClassVar[RecordKind] = RecordKind.LLM_CALL
  model: str
  usage: Any = None


@dataclass(frozen=True, kw_only=True)
class ToolCall(Record):
  kind: ClassVar[RecordKind] = RecordKind.TOOL_CALL
  call_id: str
  tool_name: str
  arguments: Any

  def __post_init__(self) -> None:
    super().__post_init__()
    if self.origin is Origin.SURFACE or self.source is None:
      raise ValueError('tool calls require scoped recorded or live provenance')
    if len(self.call_id) == 0 or len(self.tool_name) == 0:
      raise ValueError('tool call identity and name must not be empty')


@dataclass(frozen=True, kw_only=True)
class ToolResult(Record):
  kind: ClassVar[RecordKind] = RecordKind.TOOL_RESULT
  call_id: str
  result: Any
  tool_name: str | None = None
  is_error: bool = False

  def __post_init__(self) -> None:
    super().__post_init__()
    if self.origin is Origin.SURFACE or self.source is None:
      raise ValueError('tool results require scoped recorded or live provenance')
    if len(self.call_id) == 0:
      raise ValueError('tool result call_id must not be empty')
    if self.tool_name == '':
      raise ValueError('tool result tool_name must be non-empty when present')


@dataclass(frozen=True, kw_only=True)
class Error(Record):
  kind: ClassVar[RecordKind] = RecordKind.ERROR
  content: str


@dataclass(frozen=True, kw_only=True)
class HarnessEvent(Record):
  kind: ClassVar[RecordKind] = RecordKind.HARNESS_EVENT
  event: str
  body: Any


@dataclass(frozen=True, kw_only=True)
class TrailMetadata(Record):
  kind: ClassVar[RecordKind] = RecordKind.TRAIL_METADATA
  fields: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, kw_only=True)
class LaunchContextEntry(Record):
  kind: ClassVar[RecordKind] = RecordKind.LAUNCH_CONTEXT
  title: str
  content: str | None = None
  fields: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, kw_only=True)
class SegmentBoundary(Record):
  kind: ClassVar[RecordKind] = RecordKind.SEGMENT_BOUNDARY
  trail_id: str
  segment: str | None = None


@dataclass(frozen=True)
class InlineStepBody:
  value: Any


@dataclass(frozen=True)
class SpilledStepBody:
  storage_key: str
  url: str
  size: int

  def __post_init__(self) -> None:
    if len(self.storage_key) == 0 or len(self.url) == 0:
      raise ValueError('spill descriptor storage key and URL must not be empty')
    if self.size < 0:
      raise ValueError('spill descriptor size must be non-negative')


type StepBody = InlineStepBody | SpilledStepBody


@dataclass(frozen=True, kw_only=True)
class NativeStep(Record):
  kind: ClassVar[RecordKind] = RecordKind.NATIVE_STEP
  step_id: int
  step_kind: str | None
  body: StepBody
  attributes: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, kw_only=True)
class TrailListRow(Record):
  kind: ClassVar[RecordKind] = RecordKind.TRAIL_LIST_ROW
  trail_id: str
  harness: str
  owner: str | None
  model: str | None
  status: str
  subject: str | None = None
  forked_from: str | None = None


@dataclass(frozen=True, kw_only=True)
class LineageNode(Record):
  kind: ClassVar[RecordKind] = RecordKind.LINEAGE_NODE
  trail_id: str
  depth: int
  is_last: bool
  ancestor_last: tuple[bool, ...] = ()
  highlighted: bool = False
  model: str | None = None
  owner: str | None = None
  fork_step_id: int | None = None

  def __post_init__(self) -> None:
    super().__post_init__()
    if self.depth < 0:
      raise ValueError('lineage depth must be non-negative')
    if len(self.ancestor_last) not in {0, self.depth}:
      raise ValueError('lineage ancestor state must be empty or match its depth')


@dataclass(frozen=True, kw_only=True)
class Notice(Record):
  kind: ClassVar[RecordKind] = RecordKind.NOTICE
  content: str
  level: str = 'info'
  trusted_visual: bool = False


@dataclass(frozen=True, kw_only=True)
class TransientActivity(Record):
  kind: ClassVar[RecordKind] = RecordKind.TRANSIENT_ACTIVITY
  activity_id: str
  content: str
  active: bool = True

  def __post_init__(self) -> None:
    super().__post_init__()
    if self.origin is not Origin.SURFACE:
      raise ValueError('transient activity must have surface provenance')
    if len(self.activity_id) == 0:
      raise ValueError('transient activity id must not be empty')


type DisplayRecord = (
  SystemPrompt
  | UserInput
  | Reasoning
  | InterimAssistantText
  | AssistantText
  | LLMCall
  | ToolCall
  | ToolResult
  | Error
  | HarnessEvent
  | TrailMetadata
  | LaunchContextEntry
  | SegmentBoundary
  | NativeStep
  | TrailListRow
  | LineageNode
  | Notice
  | TransientActivity
)

ALL_RECORD_KINDS = frozenset(RecordKind)
CONVERSATION_RECORD_KINDS = frozenset(
  {
    RecordKind.SYSTEM_PROMPT,
    RecordKind.USER_INPUT,
    RecordKind.REASONING,
    RecordKind.INTERIM_ASSISTANT,
    RecordKind.ASSISTANT,
    RecordKind.LLM_CALL,
    RecordKind.TOOL_CALL,
    RecordKind.TOOL_RESULT,
    RecordKind.ERROR,
    RecordKind.HARNESS_EVENT,
  }
)
STRUCTURAL_RECORD_KINDS = frozenset(
  {
    RecordKind.TRAIL_METADATA,
    RecordKind.LAUNCH_CONTEXT,
    RecordKind.SEGMENT_BOUNDARY,
    RecordKind.NATIVE_STEP,
    RecordKind.TRAIL_LIST_ROW,
    RecordKind.LINEAGE_NODE,
  }
)
SURFACE_RECORD_KINDS = frozenset({RecordKind.NOTICE, RecordKind.TRANSIENT_ACTIVITY})
