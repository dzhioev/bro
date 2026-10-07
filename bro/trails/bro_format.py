"""The bro trail format."""

import json
from typing import Any

from bro.trails.backends import (
  Classification,
  OpenedBody,
  ParsedRecord,
  TrailFormat,
  projected_event,
  validate_server_derived,
)

BRO_STEP_KINDS = frozenset(
  {
    'system_prompt',
    'user_input',
    'notification',
    'tool_result',
    'llm_call',
    'error',
  }
)

# the kinds whose body a reader is entitled to read as text: they project into a
# message whose content carries no other shape, so a body that is not a string
# leaves the trail unrenderable rather than merely odd
BRO_TEXT_BODY_KINDS = frozenset({'system_prompt', 'user_input', 'notification'})


def _bro_parse(payload: Any) -> ParsedRecord:
  if not isinstance(payload, dict):
    raise ValueError('bro record must be an object')
  kind = payload.get('kind')
  if not isinstance(kind, str) or kind not in BRO_STEP_KINDS:
    raise ValueError(f'bro record kind must be one of {sorted(BRO_STEP_KINDS)}')
  body = payload.get('body')
  if kind in BRO_TEXT_BODY_KINDS and not isinstance(body, str):
    raise ValueError(f'bro {kind} body must be a string')
  timestamp = payload.get('ts')
  if timestamp is not None and not isinstance(timestamp, str):
    raise ValueError('bro record ts must be a string')
  omitted = {
    'trail_id',
    'step_id',
    'kind',
    'body',
    'body_s3',
    'ts',
    'usage',
    'raw',
    'record',
    'payload_sha256',
    'format',
  }
  attributes = {key: value for key, value in payload.items() if key not in omitted}
  return ParsedRecord(
    kind=kind,
    body=body,
    timestamp=timestamp,
    attributes=attributes,
    native=dict(payload),
  )


def _bro_classify(record: ParsedRecord) -> Classification:
  if record.kind == 'user_input':
    return Classification(turn_delta=1)
  if record.kind != 'llm_call' or not isinstance(record.body, dict):
    return Classification()
  response = record.body.get('response')
  if not isinstance(response, dict):
    return Classification()
  usage = response.get('usage')
  if not isinstance(usage, dict):
    return Classification()
  return Classification(usage_model=str(response.get('model', 'unknown')), usage=usage)


def _bro_open(body: dict) -> OpenedBody:
  unknown = set(body) - {'records'}
  if len(unknown) > 0:
    raise ValueError(f'unknown bro body fields: {sorted(unknown)}')
  records = body.get('records')
  if not isinstance(records, list):
    raise ValueError('bro body.records must be a list')
  return OpenedBody(records=records)


def _bro_validate_create(native: dict) -> None:
  validate_server_derived(native)
  if not isinstance(native.get('llm'), dict):
    raise ValueError('native.llm is required for the bro harness')
  if 'ride_command' in native and not isinstance(native['ride_command'], str):
    raise ValueError('native.ride_command must be a string')


def _bro_llm_call_messages(record: dict) -> list[dict]:
  body = record.get('body')
  response = body.get('response') if isinstance(body, dict) else None
  if not isinstance(response, dict):
    return [projected_event(record, 'harness_event', raw=record)]
  usage = record.get('usage')
  if not isinstance(usage, dict):
    raw_usage = response.get('usage')
    usage = raw_usage if isinstance(raw_usage, dict) else {}
  call_fields: dict[str, Any] = {
    'model': str(response.get('model', 'unknown')),
    'usage': usage,
  }
  if 'service_tier' in response:
    call_fields['service_tier'] = response['service_tier']
  events = [projected_event(record, 'llm_call', **call_fields)]
  output = response.get('output')
  if not isinstance(output, list):
    return events
  terminal = not any(
    isinstance(item, dict) and item.get('type') == 'function_call' for item in output
  )
  for index, item in enumerate(output, start=1):
    if not isinstance(item, dict):
      events.append(projected_event(record, 'harness_event', index, raw=item))
      continue
    item_type = item.get('type')
    if item_type == 'reasoning':
      summary = item.get('summary')
      if not isinstance(summary, list):
        events.append(projected_event(record, 'harness_event', index, raw=item))
        continue
      for part in summary:
        if isinstance(part, dict) and part.get('type') == 'summary_text':
          text = part.get('text')
          if isinstance(text, str) and len(text) > 0:
            events.append(projected_event(record, 'reasoning', index, content=text))
    elif item_type == 'message':
      content = item.get('content')
      if not isinstance(content, list):
        events.append(projected_event(record, 'harness_event', index, raw=item))
        continue
      text = ''.join(
        part.get('text', '')
        for part in content
        if isinstance(part, dict)
        and part.get('type') == 'output_text'
        and isinstance(part.get('text'), str)
      )
      if len(text) > 0:
        events.append(projected_event(record, 'assistant', index, content=text, terminal=terminal))
    elif item_type == 'function_call':
      raw_arguments = item.get('arguments')
      try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
      except json.JSONDecodeError:
        arguments = {'_raw_arguments': raw_arguments}
      events.append(
        projected_event(
          record,
          'tool_call',
          index,
          tool_name=item.get('name'),
          call_id=item.get('call_id'),
          arguments=arguments,
        )
      )
    else:
      events.append(projected_event(record, 'harness_event', index, raw=item))
  return events


def _bro_project(record: dict) -> list[dict]:
  kind = record.get('kind')
  if kind == 'llm_call':
    return _bro_llm_call_messages(record)
  if kind in {'system_prompt', 'user_input', 'notification', 'tool_result', 'error'}:
    fields: dict[str, Any] = {'content': record.get('body')}
    for key in ('tool_name', 'arguments', 'call_id', 'is_error'):
      if key in record:
        fields[key] = record[key]
    return [projected_event(record, kind, **fields)]
  return [projected_event(record, 'harness_event', raw=record)]


def _owner(header: dict) -> str | None:
  bro = header.get('bro')
  return bro if isinstance(bro, str) else None


def _native_header_fields(header: dict) -> list[tuple[str, Any]]:
  native = header.get('native')
  if not isinstance(native, dict):
    raise ValueError('trail native metadata must be an object')
  fields: list[tuple[str, Any]] = [('llm', native.get('llm', {}))]
  counts = native.get('step_counts_by_kind')
  if isinstance(counts, dict):
    fields.append(('step kinds', {key: value for key, value in counts.items() if value != 0}))
  if native.get('ride_command') is not None:
    fields.append(('ride', native['ride_command']))
  return fields


BRO_FORMAT = TrailFormat(
  name='bro',
  parse=_bro_parse,
  classify=_bro_classify,
  project=_bro_project,
  open=_bro_open,
  validate_create=_bro_validate_create,
  emitted_message_types=frozenset(
    {
      'user_input',
      'notification',
      'llm_call',
      'reasoning',
      'assistant',
      'tool_call',
      'tool_result',
      'system_prompt',
      'error',
      'harness_event',
    }
  ),
  requires_bro=True,
  owner=_owner,
  native_header_fields=_native_header_fields,
)
