"""Stateful presentation core for replayed and incremental trail records."""

import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from bro.llm.mcp import canonical_name
from bro.trails.display.blocks import (
  Append,
  BlockItem,
  BlockKind,
  PresentationBlock,
  Remove,
  Renderer,
  StyleRole,
  Update,
)
from bro.trails.display.config import (
  Appearance,
  DisplayConfig,
  Layout,
  OutputRoute,
  TimestampPolicy,
  Verbosity,
)
from bro.trails.display.records import (
  AssistantText,
  DisplayRecord,
  Error,
  HarnessEvent,
  InlineStepBody,
  InterimAssistantText,
  LaunchContextEntry,
  LineageNode,
  LiveSource,
  LLMCall,
  NativeStep,
  Notice,
  Origin,
  Reasoning,
  Record,
  RecordedSource,
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


class DisplayDataError(ValueError):
  """Malformed or inconsistent semantic display input, with known provenance."""

  def __init__(self, message: str, *, source: RecordedSource | LiveSource | None = None):
    super().__init__(message)
    self.source = source


@dataclass
class _ToolState:
  call: ToolCall | None = None
  result: ToolResult | None = None
  block_id: str | None = None
  call_visible: bool = False
  result_visible: bool = False


@dataclass
class _GroupState:
  source_group: tuple[str, int]
  style: StyleRole
  route: OutputRoute
  block: PresentationBlock


_STYLE_BY_KIND = {
  RecordKind.SYSTEM_PROMPT: StyleRole.MUTED,
  RecordKind.USER_INPUT: StyleRole.USER,
  RecordKind.REASONING: StyleRole.REASONING,
  RecordKind.INTERIM_ASSISTANT: StyleRole.ASSISTANT,
  RecordKind.ASSISTANT: StyleRole.ASSISTANT,
  RecordKind.LLM_CALL: StyleRole.METADATA,
  RecordKind.TOOL_CALL: StyleRole.TOOL,
  RecordKind.TOOL_RESULT: StyleRole.SUCCESS,
  RecordKind.ERROR: StyleRole.ERROR,
  RecordKind.HARNESS_EVENT: StyleRole.MUTED,
  RecordKind.TRAIL_METADATA: StyleRole.METADATA,
  RecordKind.LAUNCH_CONTEXT: StyleRole.METADATA,
  RecordKind.SEGMENT_BOUNDARY: StyleRole.MUTED,
  RecordKind.NATIVE_STEP: StyleRole.METADATA,
  RecordKind.TRAIL_LIST_ROW: StyleRole.METADATA,
  RecordKind.LINEAGE_NODE: StyleRole.METADATA,
  RecordKind.NOTICE: StyleRole.NOTICE,
  RecordKind.TRANSIENT_ACTIVITY: StyleRole.MUTED,
}


class DisplaySession:
  """Single-owner display state shared by replay and follow input."""

  def __init__(self, configuration: DisplayConfig, renderer: Renderer):
    self.configuration = configuration
    self.renderer = renderer
    self._owner_thread = threading.get_ident()
    self.renderer.start(configuration)
    self._closed = False
    self._record_keys: set[str] = set()
    self._last_positions: dict[tuple[Origin, str], tuple[int, int]] = {}
    self._tool_states: dict[tuple[Origin, str, str], _ToolState] = {}
    self._seen_calls: set[tuple[Origin, str, str]] = set()
    self._seen_results: set[tuple[Origin, str, str]] = set()
    self._active_transients: dict[str, str] = {}
    self._turn_numbers: dict[tuple[str, int], int] = {}
    self._next_turn = 1
    self._group: _GroupState | None = None

  def __enter__(self) -> 'DisplaySession':
    self._assert_owner()
    if self._closed:
      raise RuntimeError('display session is already closed')
    return self

  def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
    self.close()

  def consume(self, records: DisplayRecord | Iterable[DisplayRecord]) -> None:
    self._assert_open()
    batch = (records,) if isinstance(records, Record) else records
    for record in batch:
      self._consume_record(record)

  def close(self) -> None:
    self._assert_owner()
    if self._closed:
      return
    try:
      self._flush_tool_states()
      for block_id in self._active_transients.values():
        self.renderer.apply(Remove(block_id))
      self._active_transients.clear()
    finally:
      try:
        self.renderer.close()
      finally:
        self._closed = True

  def _assert_owner(self) -> None:
    if threading.get_ident() != self._owner_thread:
      raise RuntimeError('display session can only be used by its owning thread')

  def _assert_open(self) -> None:
    self._assert_owner()
    if self._closed:
      raise RuntimeError('display session is closed')

  def _consume_record(self, record: DisplayRecord) -> None:
    if record.key in self._record_keys:
      raise DisplayDataError(f'duplicate display record key: {record.key}')
    self._record_keys.add(record.key)
    self._validate_order(record)
    if isinstance(record, ToolCall):
      self._consume_tool_call(record)
      self._group = None
      return
    if isinstance(record, ToolResult):
      self._consume_tool_result(record)
      self._group = None
      return
    if isinstance(record, TransientActivity):
      self._consume_transient(record)
      self._group = None
      return
    if not self.configuration.record_filter.includes(record) or self._omit_empty(record):
      self._group = None
      return
    block = self._block_for(record)
    if self._can_group(record, block):
      assert self._group is not None
      grouped = replace(self._group.block, items=(*self._group.block.items, *block.items))
      self.renderer.apply(Update(grouped))
      self._group.block = grouped
      return
    self.renderer.apply(Append(block))
    source_group = self._source_group(record)
    self._group = (
      _GroupState(source_group, block.style, block.route, block)
      if source_group is not None and self._is_groupable(record)
      else None
    )

  def _validate_order(self, record: DisplayRecord) -> None:
    source = record.source
    if source is None:
      return
    if isinstance(source, RecordedSource):
      segment = (Origin.RECORDED, source.trail_id)
      position = (source.step_id, source.index)
    elif isinstance(source, LiveSource):
      segment = (Origin.LIVE, source.run_id)
      position = (source.sequence, 0)
    else:
      raise AssertionError(f'unhandled record source: {source!r}')
    previous = self._last_positions.get(segment)
    if previous is not None and position < previous:
      raise DisplayDataError(
        f'non-monotonic {segment[0]} source order in {segment[1]!r}: {position} follows {previous}'
      )
    self._last_positions[segment] = position

  def _consume_tool_call(self, record: ToolCall) -> None:
    identity = self._tool_identity(record)
    if identity in self._seen_calls:
      raise DisplayDataError(f'duplicate tool call: {identity[2]} in {identity[1]}')
    self._seen_calls.add(identity)
    state = self._tool_states.setdefault(identity, _ToolState())
    state.call = record
    state.call_visible = self.configuration.record_filter.includes(record)
    state.block_id = self._tool_block_id(identity)
    if state.result is None:
      if state.call_visible:
        self.renderer.apply(Append(self._tool_block(state, pending=True)))
      return
    if state.call_visible:
      self.renderer.apply(Append(self._tool_block(state, pending=False)))
    elif state.result_visible:
      self.renderer.apply(Append(self._orphan_result_block(state.result, state.block_id)))
    del self._tool_states[identity]

  def _consume_tool_result(self, record: ToolResult) -> None:
    identity = self._tool_identity(record)
    if identity in self._seen_results:
      raise DisplayDataError(f'duplicate tool result: {identity[2]} in {identity[1]}')
    self._seen_results.add(identity)
    state = self._tool_states.setdefault(identity, _ToolState())
    state.result = record
    state.result_visible = self.configuration.record_filter.includes(record)
    if state.call is None:
      return
    assert state.block_id is not None
    if state.call_visible:
      self.renderer.apply(Update(self._tool_block(state, pending=False)))
    elif state.result_visible:
      self.renderer.apply(Append(self._orphan_result_block(record, state.block_id)))
    del self._tool_states[identity]

  def _consume_transient(self, record: TransientActivity) -> None:
    block_id = f'transient:{record.activity_id}'
    previous_id = self._active_transients.get(record.activity_id)
    if not record.active:
      if previous_id is not None:
        self.renderer.apply(Remove(previous_id))
        del self._active_transients[record.activity_id]
      return
    if not self.configuration.record_filter.includes(record):
      return
    block = self._block_for(record, block_id=block_id)
    self.renderer.apply(Append(block) if previous_id is None else Update(block))
    self._active_transients[record.activity_id] = block_id

  def _flush_tool_states(self) -> None:
    for state in self._tool_states.values():
      if state.call is not None and state.call_visible:
        self.renderer.apply(Update(self._tool_block(state, pending=False, missing_result=True)))
      elif state.result is not None and state.result_visible:
        block_id = self._tool_block_id(self._tool_identity(state.result))
        self.renderer.apply(Append(self._orphan_result_block(state.result, block_id)))
    self._tool_states.clear()

  def _tool_identity(self, record: ToolCall | ToolResult) -> tuple[Origin, str, str]:
    source = record.source
    if isinstance(source, RecordedSource):
      scope = source.trail_id
    elif isinstance(source, LiveSource):
      scope = source.run_id
    else:
      raise DisplayDataError(f'tool record {record.key!r} has no correlation scope')
    return (record.origin, scope, record.call_id)

  @staticmethod
  def _tool_block_id(identity: tuple[Origin, str, str]) -> str:
    return f'tool:{identity[0]}:{len(identity[1])}:{identity[1]}:{identity[2]}'

  def _tool_block(
    self, state: _ToolState, *, pending: bool, missing_result: bool = False
  ) -> PresentationBlock:
    assert state.call is not None and state.block_id is not None
    call = state.call
    items = [self._item(call.arguments, call.kind, label='arguments')]
    style = StyleRole.TOOL
    if state.result is not None and state.result_visible:
      result_style = StyleRole.ERROR if state.result.is_error else StyleRole.SUCCESS
      items.append(
        self._item(
          state.result.result,
          state.result.kind,
          label='result',
          style=result_style,
          timestamp=self._timestamp(state.result),
        )
      )
      style = result_style if state.result.is_error else style
    elif missing_result:
      items.append(BlockItem('result unavailable', style=StyleRole.MUTED, label='result'))
    return PresentationBlock(
      id=state.block_id,
      kind=BlockKind.TOOL,
      layout=self.configuration.layout,
      route=self.configuration.routes.trace,
      style=style,
      label=sanitize_text(canonical_name(call.tool_name)),
      timestamp=self._timestamp(call),
      calendar_date=self._calendar_date(call),
      items=tuple(items),
      ordinal=self._ordinal_for(call),
      pending=pending,
    )

  def _orphan_result_block(self, record: ToolResult, block_id: str) -> PresentationBlock:
    name = canonical_name(record.tool_name) if record.tool_name is not None else record.call_id
    name = sanitize_text(name)
    style = StyleRole.ERROR if record.is_error else StyleRole.SUCCESS
    return PresentationBlock(
      id=block_id,
      kind=BlockKind.TOOL_RESULT,
      layout=self.configuration.layout,
      route=self.configuration.routes.trace,
      style=style,
      label=f'{self.configuration.labels.for_kind(record.kind)} · {name}',
      timestamp=self._timestamp(record),
      calendar_date=self._calendar_date(record),
      items=(
        self._item(
          record.result,
          record.kind,
          style=style,
          timestamp=self._timestamp(record),
        ),
      ),
      ordinal=self._ordinal_for(record),
    )

  def _block_for(self, record: DisplayRecord, *, block_id: str | None = None) -> PresentationBlock:
    kind = self._block_kind(record)
    style = _STYLE_BY_KIND[record.kind]
    items = self._items_for(record, style)
    return PresentationBlock(
      id=record.key if block_id is None else block_id,
      kind=kind,
      layout=self.configuration.layout,
      route=self._route_for(record),
      style=style,
      label=sanitize_text(self._label_for(record)),
      timestamp=self._timestamp(record),
      calendar_date=self._calendar_date(record),
      items=items,
      ordinal=self._ordinal_for(record),
      depth=record.depth if isinstance(record, LineageNode) else 0,
      tree_last=record.is_last if isinstance(record, LineageNode) else False,
      tree_ancestor_last=record.ancestor_last if isinstance(record, LineageNode) else (),
    )

  def _items_for(self, record: DisplayRecord, style: StyleRole) -> tuple[BlockItem, ...]:
    if isinstance(record, (SystemPrompt, Reasoning, InterimAssistantText, AssistantText, Error)):
      return (
        self._item(
          record.content,
          record.kind,
          style=style,
          markdown=isinstance(record, (InterimAssistantText, AssistantText)) and record.markdown,
        ),
      )
    if isinstance(record, UserInput):
      return (self._item(record.content, record.kind, style=style),)
    if isinstance(record, LLMCall):
      call_items = [self._item(record.model, record.kind, label='model', style=style)]
      if record.usage is not None:
        call_items.append(
          self._item(record.usage, record.kind, label='usage', style=StyleRole.MUTED)
        )
      return tuple(call_items)
    if isinstance(record, HarnessEvent):
      return (self._item(record.body, record.kind, label=record.event, style=style),)
    if isinstance(record, TrailMetadata):
      return tuple(
        self._item(value, record.kind, label=label, style=style) for label, value in record.fields
      )
    if isinstance(record, LaunchContextEntry):
      context_items: list[BlockItem] = []
      if record.content is not None:
        context_items.append(self._item(record.content, record.kind, style=style))
      context_items.extend(
        self._item(value, record.kind, label=label, style=style) for label, value in record.fields
      )
      return tuple(context_items)
    if isinstance(record, SegmentBoundary):
      segment_items = [BlockItem(record.trail_id, style=style, label='trail')]
      if record.segment is not None:
        segment_items.append(BlockItem(record.segment, style=style, label='segment'))
      return tuple(segment_items)
    if isinstance(record, NativeStep):
      if isinstance(record.body, InlineStepBody):
        body = self._item(
          record.body.value,
          record.kind,
          label=record.step_kind,
          style=style,
          limit=240,
        )
      elif isinstance(record.body, SpilledStepBody):
        body = BlockItem(
          f'<{record.body.size} bytes spilled> {record.body.url}',
          style=StyleRole.MUTED,
          label=record.step_kind,
        )
      else:
        raise AssertionError(f'unhandled native step body: {record.body!r}')
      attributes = tuple(
        self._item(value, record.kind, label=label, style=StyleRole.MUTED, limit=80)
        for label, value in record.attributes
      )
      return (body, *attributes)
    if isinstance(record, TrailListRow):
      trail_items = [
        BlockItem(record.harness, style=StyleRole.REASONING, label='harness'),
        BlockItem(record.owner or '?', style=StyleRole.TOOL, label='owner'),
        BlockItem(record.model or '?', style=StyleRole.MUTED, label='model'),
        BlockItem(
          record.status,
          style=(
            StyleRole.ERROR
            if record.status == 'lost'
            else StyleRole.SUCCESS
            if record.status.startswith('done:')
            else StyleRole.NOTICE
          ),
          label='status',
        ),
      ]
      if record.forked_from is not None:
        trail_items.append(BlockItem(record.forked_from, style=StyleRole.MUTED, label='fork of'))
      if record.subject is not None:
        trail_items.append(
          self._item(
            record.subject,
            record.kind,
            label='subject',
            style=StyleRole.MUTED,
            limit=60,
          )
        )
      return tuple(trail_items)
    if isinstance(record, LineageNode):
      lineage_items: list[BlockItem] = []
      if record.owner is not None:
        lineage_items.append(BlockItem(record.owner, style=style, label='owner'))
      if record.model is not None:
        lineage_items.append(BlockItem(record.model, style=style, label='model'))
      if record.fork_step_id is not None:
        lineage_items.append(BlockItem(str(record.fork_step_id), style=style, label='step'))
      if record.highlighted:
        lineage_items.append(BlockItem('here', style=StyleRole.HEADING))
      return tuple(lineage_items)
    if isinstance(record, Notice):
      return (
        self._item(
          record.content,
          record.kind,
          style=style,
          trusted_visual=record.trusted_visual,
        ),
      )
    if isinstance(record, TransientActivity):
      return (self._item(record.content, record.kind, style=style),)
    if isinstance(record, (ToolCall, ToolResult)):
      raise AssertionError('tool records use the correlation path')
    raise AssertionError(f'unhandled display record: {record!r}')

  def _item(
    self,
    value: Any,
    kind: RecordKind,
    *,
    label: str | None = None,
    style: StyleRole = StyleRole.NORMAL,
    markdown: bool = False,
    trusted_visual: bool = False,
    timestamp: str | None = None,
    limit: int | None = None,
  ) -> BlockItem:
    hidden = kind in self.configuration.hidden_content_kinds
    if trusted_visual and not isinstance(value, str):
      raise DisplayDataError('trusted visual display content must be a string')
    text = '' if hidden else value if trusted_visual else self._render_value(value, kind)
    if trusted_visual:
      content_limit = None
    elif limit is not None:
      content_limit = limit
    else:
      content_limit = self.configuration.content_limits.for_record(
        kind, self.configuration.detail_for(kind)
      )
    omitted = 0
    if not hidden and content_limit is not None and len(text) > content_limit:
      omitted = len(text) - content_limit
      text = text[:content_limit]
    return BlockItem(
      text=text,
      style=style,
      label=sanitize_text(label) if label is not None else None,
      omitted_characters=omitted,
      markdown=markdown,
      trusted_visual=trusted_visual,
      timestamp=timestamp,
    )

  def _render_value(self, value: Any, kind: RecordKind) -> str:
    if value is None:
      return ''
    if self.configuration.appearance is Appearance.CHAT:
      if kind is RecordKind.TOOL_CALL:
        return self._chat_arguments(value)
    if isinstance(value, str):
      if self.configuration.appearance is Appearance.PLAIN_LOG and kind in {
        RecordKind.TOOL_CALL,
        RecordKind.TOOL_RESULT,
      }:
        stripped = value.strip()
        if len(stripped) > 0 and stripped[0] in '[{':
          try:
            value = json.loads(stripped)
          except json.JSONDecodeError:
            return sanitize_text(value)
        else:
          return sanitize_text(value)
      else:
        return sanitize_text(value)
    indent = None if self.configuration.appearance is Appearance.REWIND else 2
    try:
      rendered = json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False)
    except (TypeError, ValueError) as exception:
      raise DisplayDataError(f'display value is not JSON-serializable: {value!r}') from exception
    return sanitize_text(rendered)

  @staticmethod
  def _chat_arguments(value: Any) -> str:
    if not isinstance(value, dict):
      raise DisplayDataError('chat tool arguments must be an object')
    arguments = []
    for name, argument in value.items():
      if isinstance(argument, str):
        compact = ' '.join(argument.split())
      else:
        try:
          compact = json.dumps(argument, ensure_ascii=False, separators=(',', ':'))
        except (TypeError, ValueError) as exception:
          raise DisplayDataError(
            f'display value is not JSON-serializable: {argument!r}'
          ) from exception
      rendered = compact if len(compact) <= 10 else '...'
      arguments.append(f'{sanitize_text(str(name))}={sanitize_text(rendered)}')
    return ', '.join(arguments)

  def _ordinal_for(self, record: DisplayRecord) -> int | None:
    if self.configuration.appearance is not Appearance.REWIND or not isinstance(
      record,
      (UserInput, Reasoning, InterimAssistantText, AssistantText, ToolCall, ToolResult, Error),
    ):
      return None
    source = record.source
    if not isinstance(source, RecordedSource):
      return None
    source_group = (source.trail_id, source.step_id)
    ordinal = self._turn_numbers.get(source_group)
    if ordinal is None:
      ordinal = self._next_turn
      self._next_turn += 1
      self._turn_numbers[source_group] = ordinal
    return ordinal

  def _label_for(self, record: DisplayRecord) -> str:
    label = self.configuration.labels.for_kind(record.kind)
    if isinstance(record, UserInput):
      markers = []
      if record.is_sidechain:
        markers.append('[sub]')
      if record.is_meta:
        markers.append('[meta]')
      if len(markers) > 0:
        label = f'{" ".join(markers)} {label}'
    if isinstance(record, LaunchContextEntry):
      return record.title
    if isinstance(record, TrailListRow):
      return record.trail_id
    if isinstance(record, LineageNode):
      return record.trail_id
    if isinstance(record, NativeStep):
      return f'{label} {record.step_id}'
    if isinstance(record, Notice) and record.level != 'info':
      return f'{label} · {record.level}'
    return label

  def _timestamp(self, record: DisplayRecord) -> str | None:
    timestamp, _calendar_date = self._time_parts(record)
    return timestamp

  def _calendar_date(self, record: DisplayRecord) -> str | None:
    _timestamp, calendar_date = self._time_parts(record)
    return calendar_date

  def _time_parts(self, record: DisplayRecord) -> tuple[str | None, str | None]:
    if self.configuration.timestamps is TimestampPolicy.HIDDEN:
      return None, None
    if record.timestamp is not None:
      timestamp = sanitize_text(record.timestamp)
      if self.configuration.appearance is Appearance.REWIND:
        return timestamp.replace('T', ' ')[:19], None
      if 'T' in timestamp:
        try:
          moment = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone()
        except ValueError:
          pass
        else:
          return moment.strftime('%H:%M:%S'), moment.date().isoformat()
      return timestamp, None
    if self.configuration.timestamps is TimestampPolicy.PLACEHOLDER:
      return '-', None
    return None, None

  def _route_for(self, record: DisplayRecord) -> OutputRoute:
    routes = self.configuration.routes
    if isinstance(record, AssistantText):
      return routes.reply
    if isinstance(record, (SystemPrompt, UserInput, InterimAssistantText)):
      return routes.conversation
    if isinstance(record, (TrailMetadata, LaunchContextEntry, SegmentBoundary, NativeStep)):
      return routes.metadata
    if isinstance(record, (TrailListRow, LineageNode)):
      return routes.metadata
    if isinstance(record, (Notice, TransientActivity)):
      return routes.status
    return routes.trace

  @staticmethod
  def _block_kind(record: DisplayRecord) -> BlockKind:
    if isinstance(
      record, (SystemPrompt, UserInput, Reasoning, InterimAssistantText, AssistantText, Error)
    ):
      return BlockKind.MESSAGE
    if isinstance(record, (LLMCall, HarnessEvent)):
      return BlockKind.EVENT
    if isinstance(record, TrailMetadata):
      return BlockKind.METADATA
    if isinstance(record, LaunchContextEntry):
      return BlockKind.CONTEXT
    if isinstance(record, SegmentBoundary):
      return BlockKind.SEGMENT
    if isinstance(record, NativeStep):
      return BlockKind.NATIVE_STEP
    if isinstance(record, TrailListRow):
      return BlockKind.TRAIL_ROW
    if isinstance(record, LineageNode):
      return BlockKind.LINEAGE_NODE
    if isinstance(record, Notice):
      return BlockKind.NOTICE
    if isinstance(record, TransientActivity):
      return BlockKind.STATUS
    raise AssertionError(f'unhandled block kind for {record!r}')

  def _omit_empty(self, record: DisplayRecord) -> bool:
    return (
      isinstance(record, (Reasoning, InterimAssistantText, AssistantText))
      and len(record.content.strip()) == 0
      and self.configuration.detail_for(record.kind) is not Verbosity.DEBUG
    )

  def _can_group(self, record: DisplayRecord, block: PresentationBlock) -> bool:
    if self._group is None or not self._is_groupable(record):
      return False
    source_group = self._source_group(record)
    return (
      source_group == self._group.source_group
      and block.style is self._group.style
      and block.route is self._group.route
    )

  def _is_groupable(self, record: DisplayRecord) -> bool:
    return self.configuration.layout is Layout.CONVERSATION and isinstance(
      record, (Reasoning, InterimAssistantText, AssistantText, Error)
    )

  @staticmethod
  def _source_group(record: DisplayRecord) -> tuple[str, int] | None:
    source = record.source
    if isinstance(source, RecordedSource):
      return (source.trail_id, source.step_id)
    return None


def sanitize_text(text: str) -> str:
  """Neutralize terminal control bytes while preserving newlines and tabs."""
  return ''.join(
    character
    if character in {'\n', '\t'} or ord(character) >= 32 and ord(character) != 127
    else '�'
    for character in text
  )
