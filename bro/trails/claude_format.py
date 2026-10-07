"""The Claude trail format."""

from typing import Any, Optional

from bro.trails import claude_lineage
from bro.trails.backends import (
  Classification,
  OpenedBody,
  ParsedRecord,
  TrailFormat,
  parse_json_object,
  projected_event,
  validate_server_derived,
)

_CLAUDE_TASK_NOTIFICATION = 'task-notification'


def _claude_parse(payload: Any) -> ParsedRecord:
  raw = payload.get('body') if isinstance(payload, dict) else payload
  if not isinstance(raw, str):
    raise ValueError('claude record must be a raw JSONL string')
  record = parse_json_object(raw)
  timestamp = record.get('timestamp') if isinstance(record, dict) else None
  if timestamp is not None and not isinstance(timestamp, str):
    timestamp = None
  kind_value = record.get('type') if isinstance(record, dict) else None
  kind = kind_value if isinstance(kind_value, str) else None
  attributes: dict[str, Any] = {}
  if isinstance(record, dict):
    for key in ('uuid', 'isSidechain', 'isMeta'):
      if key in record:
        attributes[key] = record[key]
    message = record.get('message')
    message_id = message.get('id') if isinstance(message, dict) else None
    if isinstance(message_id, str):
      attributes['message_id'] = message_id
  return ParsedRecord(
    kind=kind,
    body=raw,
    timestamp=timestamp,
    attributes=attributes,
    native={'raw': raw, 'record': record},
  )


def _claude_tool_results_only(message: dict) -> bool:
  content = message.get('content')
  return isinstance(content, list) and all(
    isinstance(block, dict) and block.get('type') == 'tool_result' for block in content
  )


def _claude_classify(record: ParsedRecord) -> Classification:
  native = record.native.get('record')
  if not isinstance(native, dict):
    return Classification()
  native_updates: dict[str, Any] = {}
  version = native.get('version')
  if isinstance(version, str):
    native_updates['harness_version'] = version
  if record.kind == 'ai-title':
    title = native.get('aiTitle')
    return Classification(
      native_updates=native_updates,
      subject=title if isinstance(title, str) and len(title) > 0 else None,
    )
  message = native.get('message')
  if record.kind == 'user' and isinstance(message, dict):
    turn_delta = int(native.get('isMeta') is not True and not _claude_tool_results_only(message))
    return Classification(turn_delta=turn_delta, native_updates=native_updates)
  if record.kind != 'assistant' or not isinstance(message, dict):
    return Classification(native_updates=native_updates)
  usage = message.get('usage')
  model = str(message.get('model', 'unknown'))
  if (
    not isinstance(usage, dict) or model == '<synthetic>' or native.get('isApiErrorMessage') is True
  ):
    return Classification(native_updates=native_updates)
  message_id = message.get('id')
  return Classification(
    usage_model=model,
    usage=usage,
    billing_key=message_id if isinstance(message_id, str) else None,
    native_updates=native_updates,
  )


def _claude_open(body: dict) -> OpenedBody:
  unknown = set(body) - {'records'}
  if len(unknown) > 0:
    raise ValueError(f'unknown claude body fields: {sorted(unknown)}')
  records = body.get('records')
  if not isinstance(records, list) or not all(isinstance(record, str) for record in records):
    raise ValueError('claude body.records must be a list of strings')
  return OpenedBody(records=records)


def _claude_validate_create(native: dict) -> None:
  validate_server_derived(native)
  for field, expected_type in (
    ('llm', dict),
    ('segment', str),
    ('ride_command', str),
    ('harness_version', str),
  ):
    if not isinstance(native.get(field), expected_type):
      raise ValueError(f'native.{field} is required for the claude harness')


def _claude_assistant_messages(record: dict, native: dict, message: dict) -> list[dict]:
  if native.get('isApiErrorMessage') is True:
    return [projected_event(record, 'error', content=message.get('content'))]
  model = str(message.get('model', 'unknown'))
  raw_usage = message.get('usage')
  if not isinstance(raw_usage, dict) or model == '<synthetic>':
    return [projected_event(record, 'harness_event', raw=native)]
  events: list[dict] = []
  contributed_usage = record.get('usage')
  if isinstance(contributed_usage, dict):
    events.append(projected_event(record, 'llm_call', model=model, usage=contributed_usage))
  content = message.get('content')
  if not isinstance(content, list):
    return [*events, projected_event(record, 'harness_event', 1, raw=native)]
  for index, block in enumerate(content, start=1):
    if not isinstance(block, dict):
      events.append(projected_event(record, 'harness_event', index, raw=block))
    elif block.get('type') == 'thinking':
      events.append(projected_event(record, 'reasoning', index, content=block.get('thinking')))
    elif block.get('type') == 'text':
      events.append(projected_event(record, 'assistant', index, content=block.get('text')))
    elif block.get('type') == 'tool_use':
      events.append(
        projected_event(
          record,
          'tool_call',
          index,
          tool_name=block.get('name'),
          call_id=block.get('id'),
          arguments=block.get('input'),
        )
      )
    else:
      events.append(projected_event(record, 'harness_event', index, raw=block))
  return events


def _claude_user_messages(record: dict, native: dict, message: dict) -> list[dict]:
  content = message.get('content')
  if _claude_tool_results_only(message):
    assert isinstance(content, list)
    return [
      projected_event(
        record,
        'tool_result',
        index,
        call_id=block.get('tool_use_id'),
        content=block.get('content'),
        is_error=block.get('is_error', False),
      )
      for index, block in enumerate(content)
      if isinstance(block, dict)
    ]
  if _claude_origin_kind(native.get('origin')) == _CLAUDE_TASK_NOTIFICATION:
    return [
      projected_event(record, 'notification', content=content, event=_CLAUDE_TASK_NOTIFICATION)
    ]
  return [
    projected_event(
      record,
      'user_input',
      content=content,
      isMeta=native.get('isMeta', False),
      isSidechain=native.get('isSidechain', False),
      # claude writes an interrupt as a user-role notice of its own, so what
      # separates it from something a human typed is the message it interrupted
      interrupted='interruptedMessageId' in native,
    )
  ]


def _claude_origin_kind(origin: Any) -> Optional[str]:
  kind = origin.get('kind') if isinstance(origin, dict) else None
  return kind if isinstance(kind, str) else None


def _claude_queued_messages(record: dict, native: dict, queued: dict) -> list[dict]:
  """a prompt claude delivered mid-turn, projected as the `user` record it
  writes for one that opens a turn."""
  # claude's own reading: a queued command without an origin that runs in
  # task-notification mode is a task notification
  origin_kind = _claude_origin_kind(queued.get('origin'))
  if origin_kind is None and queued.get('commandMode') == _CLAUDE_TASK_NOTIFICATION:
    origin_kind = _CLAUDE_TASK_NOTIFICATION
  if origin_kind == _CLAUDE_TASK_NOTIFICATION:
    return [
      projected_event(
        record, 'notification', content=queued.get('prompt'), event=_CLAUDE_TASK_NOTIFICATION
      )
    ]
  return [
    projected_event(
      record,
      'user_input',
      content=queued.get('prompt'),
      isMeta=queued.get('isMeta') is True,
      isSidechain=native.get('isSidechain', False),
      interrupted=False,
    )
  ]


def _claude_attachment_messages(record: dict, native: dict, attachment: dict) -> list[dict]:
  attachment_type = attachment.get('type')
  if not isinstance(attachment_type, str):
    return [projected_event(record, 'harness_event', raw=native)]
  if attachment_type == 'queued_command':
    return _claude_queued_messages(record, native, attachment)
  payload = {key: value for key, value in attachment.items() if key != 'type'}
  return [projected_event(record, 'notification', content=payload, event=attachment_type)]


def _claude_project(record: dict) -> list[dict]:
  native = _claude_parse(record).native['record']
  if not isinstance(native, dict):
    return [projected_event(record, 'harness_event', raw=record.get('body'))]
  message = native.get('message')
  if native.get('type') == 'assistant' and isinstance(message, dict):
    return _claude_assistant_messages(record, native, message)
  if native.get('type') == 'user' and isinstance(message, dict):
    return _claude_user_messages(record, native, message)
  attachment = native.get('attachment')
  if native.get('type') == 'attachment' and isinstance(attachment, dict):
    return _claude_attachment_messages(record, native, attachment)
  return [projected_event(record, 'harness_event', raw=native)]


def _owner(header: dict) -> str | None:
  location = header.get('location')
  if not isinstance(location, dict):
    return None
  workspace = location.get('workspace')
  return workspace if isinstance(workspace, str) else None


def _native_header_fields(header: dict) -> list[tuple[str, Any]]:
  native = header.get('native')
  if not isinstance(native, dict):
    raise ValueError('trail native metadata must be an object')
  fields: list[tuple[str, Any]] = [
    ('llm', native.get('llm', {})),
    ('claude-code', native.get('harness_version', '?')),
    ('lines', header.get('extent', '?')),
    ('segment', native.get('segment', '?')),
  ]
  if native.get('ride_command') is not None:
    fields.append(('ride', native['ride_command']))
  elif native.get('cw_command') is not None:
    fields.append(('cw', native['cw_command']))
  return fields


CLAUDE_FORMAT = TrailFormat(
  name='claude',
  parse=_claude_parse,
  classify=_claude_classify,
  project=_claude_project,
  open=_claude_open,
  validate_create=_claude_validate_create,
  resolve_lineage=claude_lineage.resolve,
  emitted_message_types=frozenset(
    {
      'user_input',
      'notification',
      'llm_call',
      'reasoning',
      'assistant',
      'tool_call',
      'tool_result',
      'error',
      'harness_event',
    }
  ),
  requires_bro=False,
  owner=_owner,
  native_header_fields=_native_header_fields,
)
