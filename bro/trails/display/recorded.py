"""Adapt trails-server read responses into semantic display records."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from bro.trails.display.core import DisplayDataError
from bro.trails.display.records import (
  AssistantText,
  DisplayRecord,
  Error,
  HarnessEvent,
  InlineStepBody,
  InterimAssistantText,
  LaunchContextEntry,
  LineageNode,
  LLMCall,
  NativeStep,
  Origin,
  Reasoning,
  RecordedSource,
  SegmentBoundary,
  SpilledStepBody,
  SystemPrompt,
  ToolCall,
  ToolResult,
  TrailListRow,
  TrailMetadata,
  UserInput,
)
from bro.trails.lineage import walk_header_chain
from bro.trails.model import MESSAGE_TYPES, UNREPORTED_END_INFERENCE, spill_descriptor
from bro.trails.store import TrailsStore


def _require_string(value: Any, name: str, *, nonempty: bool = False) -> str:
  if not isinstance(value, str) or nonempty and len(value) == 0:
    qualifier = 'a non-empty string' if nonempty else 'a string'
    raise ValueError(f'{name} must be {qualifier}')
  return value


def _require_integer(value: Any, name: str) -> int:
  if not isinstance(value, int) or isinstance(value, bool) or value < 0:
    raise ValueError(f'{name} must be a non-negative integer')
  return value


def _text_content(value: Any, name: str) -> str:
  if isinstance(value, str):
    return value
  if not isinstance(value, list):
    raise ValueError(f'{name} must be a string or content-block list')
  parts: list[str] = []
  for block in value:
    if not isinstance(block, dict):
      raise ValueError(f'{name} content blocks must be objects')
    if block.get('type') != 'text':
      continue
    parts.append(_require_string(block.get('text'), f'{name} text block'))
  return '\n'.join(parts)


def _error_content(value: Any, name: str) -> Any:
  if isinstance(value, (str, list)):
    return _text_content(value, name)
  return value


@contextmanager
def _trail_provenance(trail_id: str) -> Iterator[None]:
  """Malformed recorded data leaves as a display error naming the trail it came from."""
  try:
    yield
  except DisplayDataError:
    raise
  except (KeyError, TypeError, ValueError) as exception:
    raise DisplayDataError(f'malformed recorded trail {trail_id}: {exception}') from exception


def _format_timestamp(timestamp: Any) -> str:
  if timestamp is None:
    return '-'
  return _require_string(timestamp, 'timestamp', nonempty=True).replace('T', ' ')[:19]


def _parse_timestamp(timestamp: str) -> datetime | None:
  try:
    return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
  except ValueError:
    return None


def _format_duration(seconds: float) -> str:
  whole_seconds = int(seconds)
  if whole_seconds < 60:
    return f'{whole_seconds}s'
  if whole_seconds < 3600:
    return f'{whole_seconds // 60}m {whole_seconds % 60}s'
  hours, remainder = divmod(whole_seconds, 3600)
  return f'{hours}h {remainder // 60}m'


def _format_end(header: dict[str, Any]) -> str:
  end = header.get('end')
  if end is None:
    return 'live'
  if not isinstance(end, dict):
    raise ValueError('trail end must be an object or null')
  reason = end.get('reason')
  if reason is None and end.get('inference') == UNREPORTED_END_INFERENCE:
    reason = UNREPORTED_END_INFERENCE
  reason = _require_string(reason, 'trail end reason', nonempty=True)
  ended_at = end.get('at')
  duration = ''
  started_at = header.get('started_at')
  if isinstance(started_at, str) and isinstance(ended_at, str):
    start_moment = _parse_timestamp(started_at)
    end_moment = _parse_timestamp(ended_at)
    if start_moment is not None and end_moment is not None:
      duration = f', {_format_duration((end_moment - start_moment).total_seconds())}'
  return f'{_format_timestamp(ended_at)} ({reason}{duration})'


def _format_pointer(pointer: Any) -> str:
  if not isinstance(pointer, dict):
    raise ValueError('trail pointer must be an object')
  trail_id = _require_string(pointer.get('trail_id'), 'trail pointer trail_id', nonempty=True)
  step_id = _require_integer(pointer.get('step_id'), 'trail pointer step_id')
  rendered = f'{trail_id} @ step {step_id}'
  if 'index' in pointer and pointer['index'] is not None:
    rendered += f':{_require_integer(pointer["index"], "trail pointer index")}'
  return rendered


def _owner(header: dict[str, Any]) -> str | None:
  if header.get('harness') == 'claude':
    location = header.get('location')
    if not isinstance(location, dict):
      return None
    workspace = location.get('workspace')
    return workspace if isinstance(workspace, str) else None
  bro = header.get('bro')
  return bro if isinstance(bro, str) else None


def _model(header: dict[str, Any]) -> str | None:
  native = header.get('native')
  llm = native.get('llm') if isinstance(native, dict) else None
  model = llm.get('model') if isinstance(llm, dict) else None
  return model if isinstance(model, str) else None


def _native_header_fields(header: dict[str, Any]) -> list[tuple[str, Any]]:
  harness = header['harness']
  native = header.get('native')
  if not isinstance(native, dict):
    raise ValueError('trail native metadata must be an object')
  fields: list[tuple[str, Any]] = [('llm', native.get('llm', {}))]
  if harness == 'bro':
    counts = native.get('step_counts_by_kind')
    if isinstance(counts, dict):
      fields.append(('step kinds', {key: value for key, value in counts.items() if value != 0}))
  elif harness == 'claude':
    fields.extend(
      [
        ('claude-code', native.get('harness_version', '?')),
        ('lines', header.get('extent', '?')),
        ('segment', native.get('segment', '?')),
      ]
    )
    if native.get('ride_command') is not None:
      fields.append(('ride', native['ride_command']))
    # trails blazed by `cw`, the runtime preceding `ride`, carry the launch
    # command under its own name
    elif native.get('cw_command') is not None:
      fields.append(('cw', native['cw_command']))
  else:
    fields.append(('native', native))
  return fields


class RecordedAdapter:
  """Stateful converter for one recorded display session."""

  def __init__(self, client: TrailsStore):
    self.client = client
    self._source_occurrences: dict[tuple[str, int, int], int] = {}

  def message_records(
    self, trail_id: str, messages: Iterable[dict[str, Any]]
  ) -> list[DisplayRecord]:
    records: list[DisplayRecord] = []
    for message in messages:
      records.append(self._message_record(trail_id, message))
    return records

  def conversation_records(self, header: dict[str, Any]) -> list[DisplayRecord]:
    target_id = _require_string(header.get('id'), 'trail id', nonempty=True)
    with _trail_provenance(target_id):
      records: list[DisplayRecord] = [self.trail_metadata(header)]
      records.extend(
        self.launch_context_records(target_id, self.client.get_launch_context(target_id))
      )
      segments = walk_header_chain(header, self.client.get_trail)
      for segment_index, (segment_header, bound) in enumerate(segments):
        segment_id = _require_string(segment_header.get('id'), 'segment trail id', nonempty=True)
        if segment_index > 0:
          native = segment_header.get('native')
          segment = native.get('segment') if isinstance(native, dict) else None
          if segment is not None:
            segment = _require_string(segment, 'segment id', nonempty=True)
          records.append(
            SegmentBoundary(
              key=f'recorded:{segment_id}:segment-boundary',
              origin=Origin.RECORDED,
              timestamp=(
                segment_header.get('started_at')
                if isinstance(segment_header.get('started_at'), str)
                else None
              ),
              trail_id=segment_id,
              segment=segment,
            )
          )
        messages = list(self._bounded_messages(segment_id, bound))
        records.extend(self.message_records(segment_id, messages))
      return records

  def native_step_records(self, trail_id: str, steps: Iterable[dict[str, Any]]) -> list[NativeStep]:
    return [self.native_step(trail_id, step) for step in steps]

  def trail_metadata(self, header: dict[str, Any]) -> TrailMetadata:
    trail_id = _require_string(header.get('id'), 'trail id', nonempty=True)
    harness = _require_string(header.get('harness'), 'trail harness', nonempty=True)
    fields: list[tuple[str, Any]] = [
      ('trail', trail_id),
      ('harness', harness),
      ('started', _format_timestamp(header.get('started_at'))),
      ('ended', _format_end(header)),
      ('bro', header.get('bro')),
      ('version', header.get('version')),
      ('interactive', header.get('interactive')),
      ('surface', header.get('surface')),
    ]
    location = header.get('location')
    if isinstance(location, dict):
      fields.extend((key, location[key]) for key in ('workspace', 'host') if key in location)
    if header.get('subject') is not None:
      fields.append(('subject', header['subject']))
    if header.get('forked_from') is not None:
      fields.append(('forked from', _format_pointer(header['forked_from'])))
    if header.get('summoned_by') is not None:
      fields.append(('summoned by', header['summoned_by']))
    fields.extend(
      [
        ('turns', header.get('turn_count')),
        ('usage', header.get('usage', {})),
        ('models', header.get('models', [])),
      ]
    )
    fields.extend(_native_header_fields(header))
    return TrailMetadata(
      key=f'recorded:{trail_id}:metadata',
      origin=Origin.RECORDED,
      timestamp=header.get('started_at') if isinstance(header.get('started_at'), str) else None,
      fields=tuple(fields),
    )

  def launch_context_records(self, trail_id: str, launch_context: Any) -> list[LaunchContextEntry]:
    if launch_context is None:
      return []
    if not isinstance(launch_context, list):
      raise DisplayDataError(f'trail {trail_id!r} launch context must be a list')
    records = []
    for index, entry in enumerate(launch_context):
      if not isinstance(entry, dict):
        raise DisplayDataError(f'trail {trail_id!r} launch context entry {index} must be an object')
      title = entry.get('title')
      if not isinstance(title, str) or len(title) == 0:
        title = f'{entry.get("kind", "?")}/{entry.get("subtype", "?")}'
      content = entry.get('content')
      if content is not None and not isinstance(content, str):
        raise DisplayDataError(
          f'trail {trail_id!r} launch context entry {index} content must be a string'
        )
      fields = entry.get('fields', {})
      if not isinstance(fields, dict):
        raise DisplayDataError(
          f'trail {trail_id!r} launch context entry {index} fields must be an object'
        )
      records.append(
        LaunchContextEntry(
          key=f'recorded:{trail_id}:context:{index}',
          origin=Origin.RECORDED,
          title=title,
          content=content,
          fields=tuple(fields.items()),
        )
      )
    return records

  def native_step(self, trail_id: str, step: dict[str, Any]) -> NativeStep:
    try:
      step_id = _require_integer(step.get('step_id'), 'step_id')
      timestamp = step.get('ts')
      if timestamp is not None:
        timestamp = _require_string(timestamp, 'step timestamp', nonempty=True)
      kind = step.get('kind')
      if kind is not None:
        kind = _require_string(kind, 'step kind', nonempty=True)
      body_value = step.get('body')
      if kind == 'end' and isinstance(body_value, dict) and body_value.get('reason') == 'terminal':
        body_value = {**body_value, 'reason': 'ok'}
      descriptor = spill_descriptor(body_value)
      body = (
        SpilledStepBody(
          storage_key=descriptor['s3'],
          url=descriptor['url'],
          size=descriptor['size'],
        )
        if descriptor is not None
        else InlineStepBody(body_value)
      )
      omitted = {
        'trail_id',
        'step_id',
        'kind',
        'ts',
        'turn_index',
        'body',
        'where',
      }
      attributes = []
      if 'turn_index' in step:
        attributes.append(('turn_index', step['turn_index']))
      attributes.extend((key, value) for key, value in step.items() if key not in omitted)
      return NativeStep(
        key=f'recorded:{trail_id}:step:{step_id}',
        origin=Origin.RECORDED,
        source=RecordedSource(trail_id, step_id),
        timestamp=timestamp,
        step_id=step_id,
        step_kind=kind,
        body=body,
        attributes=tuple(attributes),
      )
    except (KeyError, TypeError, ValueError) as exception:
      raise DisplayDataError(
        f'malformed native step in trail {trail_id!r}: {exception}'
      ) from exception

  def trail_list_row(self, header: dict[str, Any]) -> TrailListRow:
    trail_id = _require_string(header.get('id'), 'trail id', nonempty=True)
    harness = _require_string(header.get('harness'), 'trail harness', nonempty=True)
    end = header.get('end')
    if end is None:
      status = 'live'
    elif not isinstance(end, dict):
      raise DisplayDataError(f'trail {trail_id!r} end must be an object or null')
    elif end.get('inference') == UNREPORTED_END_INFERENCE:
      status = UNREPORTED_END_INFERENCE
    elif end.get('reason') == 'lost':
      status = 'lost'
    else:
      status = f'done:{_require_string(end.get("reason"), "trail end reason", nonempty=True)}'
    forked_from = header.get('forked_from')
    forked_from_id = None
    if forked_from is not None:
      if not isinstance(forked_from, dict):
        raise DisplayDataError(f'trail {trail_id!r} forked_from must be an object')
      forked_from_id = _require_string(
        forked_from.get('trail_id'), 'forked_from trail_id', nonempty=True
      )
    subject = header.get('subject')
    if subject is not None and not isinstance(subject, str):
      raise DisplayDataError(f'trail {trail_id!r} subject must be a string')
    return TrailListRow(
      key=f'recorded:{trail_id}:list-row',
      origin=Origin.RECORDED,
      timestamp=header.get('started_at') if isinstance(header.get('started_at'), str) else None,
      trail_id=trail_id,
      harness=harness,
      owner=_owner(header),
      model=_model(header),
      status=status,
      subject=subject,
      forked_from=forked_from_id,
    )

  def lineage_records(self, trail_id: str) -> list[LineageNode]:
    selected = self.client.get_trail(trail_id)
    root = walk_header_chain(selected, self.client.get_trail)[0][0]
    records: list[LineageNode] = []

    def collect(
      header: dict[str, Any], depth: int, is_last: bool, ancestor_last: tuple[bool, ...]
    ) -> None:
      current_id = _require_string(header.get('id'), 'trail id', nonempty=True)
      forked_from = header.get('forked_from')
      fork_step_id = None
      if forked_from is not None:
        if not isinstance(forked_from, dict):
          raise DisplayDataError(f'trail {current_id!r} forked_from must be an object')
        fork_step_id = _require_integer(forked_from.get('step_id'), 'forked_from step_id')
      records.append(
        LineageNode(
          key=f'recorded:{current_id}:lineage',
          origin=Origin.RECORDED,
          trail_id=current_id,
          depth=depth,
          is_last=is_last,
          ancestor_last=ancestor_last,
          highlighted=current_id == trail_id,
          model=_model(header),
          owner=_owner(header),
          fork_step_id=fork_step_id,
        )
      )
      children = list(self.client.iter_trails(forked_from=current_id))
      children.reverse()
      child_ancestors = (*ancestor_last, is_last)
      for index, child in enumerate(children):
        collect(child, depth + 1, index == len(children) - 1, child_ancestors)

    collect(root, 0, True, ())
    return records

  def _bounded_messages(
    self, trail_id: str, bound: dict[str, Any] | None
  ) -> Iterable[dict[str, Any]]:
    if bound is None:
      yield from self.client.iter_messages(trail_id)
      return
    bound_step_id = _require_integer(bound.get('step_id'), 'fork bound step_id')
    bound_index_value = bound.get('index')
    bound_index = (
      None if bound_index_value is None else _require_integer(bound_index_value, 'fork bound index')
    )
    for message in self.client.iter_messages(trail_id):
      source = self._message_source(trail_id, message)
      if source.step_id > bound_step_id:
        return
      if source.step_id == bound_step_id and bound_index is not None and source.index > bound_index:
        return
      yield message

  def _message_record(self, trail_id: str, message: dict[str, Any]) -> DisplayRecord:
    source = self._message_source(trail_id, message)
    try:
      message_type = _require_string(message.get('type'), 'message type', nonempty=True)
      if message_type not in MESSAGE_TYPES:
        raise ValueError(f'unknown message type {message_type!r}')
      timestamp = message.get('ts')
      if timestamp is not None:
        timestamp = _require_string(timestamp, 'message timestamp', nonempty=True)
      occurrence_key = (trail_id, source.step_id, source.index)
      occurrence = self._source_occurrences.get(occurrence_key, 0)
      self._source_occurrences[occurrence_key] = occurrence + 1
      common = {
        'key': (f'recorded:{trail_id}:message:{source.step_id}:{source.index}:{occurrence}'),
        'origin': Origin.RECORDED,
        'source': source,
        'timestamp': timestamp,
      }
      if message_type == 'system_prompt':
        return SystemPrompt(
          content=_text_content(message.get('content'), 'system prompt'), **common
        )
      if message_type == 'user_input':
        is_meta = message.get('isMeta', False)
        is_sidechain = message.get('isSidechain', False)
        if not isinstance(is_meta, bool) or not isinstance(is_sidechain, bool):
          raise ValueError('user markers must be booleans')
        return UserInput(
          content=_text_content(message.get('content'), 'user input'),
          is_meta=is_meta,
          is_sidechain=is_sidechain,
          **common,
        )
      if message_type == 'reasoning':
        return Reasoning(content=_text_content(message.get('content'), 'reasoning'), **common)
      if message_type == 'assistant':
        content = _text_content(message.get('content'), 'assistant content')
        terminal = message.get('terminal')
        if terminal is not None and not isinstance(terminal, bool):
          raise ValueError('assistant terminal must be a boolean when present')
        record_type = InterimAssistantText if terminal is False else AssistantText
        return record_type(content=content, **common)
      if message_type == 'llm_call':
        return LLMCall(
          model=_require_string(message.get('model'), 'llm_call model', nonempty=True),
          usage=message.get('usage'),
          **common,
        )
      if message_type == 'tool_call':
        if 'arguments' not in message:
          raise ValueError('tool_call arguments are required')
        return ToolCall(
          call_id=_require_string(message.get('call_id'), 'tool_call call_id', nonempty=True),
          tool_name=_require_string(message.get('tool_name'), 'tool_call tool_name', nonempty=True),
          arguments=message['arguments'],
          **common,
        )
      if message_type == 'tool_result':
        if 'content' not in message:
          raise ValueError('tool_result content is required')
        tool_name = message.get('tool_name')
        if tool_name is not None:
          tool_name = _require_string(tool_name, 'tool_result tool_name', nonempty=True)
        is_error = message.get('is_error', False)
        if not isinstance(is_error, bool):
          raise ValueError('tool_result is_error must be a boolean')
        return ToolResult(
          call_id=_require_string(message.get('call_id'), 'tool_result call_id', nonempty=True),
          tool_name=tool_name,
          result=message['content'],
          is_error=is_error,
          **common,
        )
      if message_type == 'error':
        return Error(content=_error_content(message.get('content'), 'error content'), **common)
      if message_type == 'harness_event':
        if 'raw' not in message:
          raise ValueError('harness_event raw body is required')
        raw = message['raw']
        event = raw.get('type') if isinstance(raw, dict) else None
        return HarnessEvent(
          event=event if isinstance(event, str) else 'harness_event', body=raw, **common
        )
      raise AssertionError(f'unhandled known message type: {message_type}')
    except (KeyError, TypeError, ValueError) as exception:
      raise DisplayDataError(
        f'malformed recorded message at {trail_id}:{source.step_id}:{source.index}: {exception}',
        source=source,
      ) from exception

  @staticmethod
  def _message_source(trail_id: str, message: dict[str, Any]) -> RecordedSource:
    if not isinstance(message, dict):
      raise DisplayDataError(f'malformed recorded message in trail {trail_id!r}: expected object')
    source = message.get('source')
    if not isinstance(source, dict):
      raise DisplayDataError(
        f'malformed recorded message in trail {trail_id!r}: source must be an object'
      )
    try:
      return RecordedSource(
        trail_id,
        _require_integer(source.get('step_id'), 'message source step_id'),
        _require_integer(source.get('index', 0), 'message source index'),
      )
    except ValueError as exception:
      raise DisplayDataError(
        f'malformed recorded message in trail {trail_id!r}: {exception}'
      ) from exception
