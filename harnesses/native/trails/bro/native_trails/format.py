"""The bro trail format."""

from typing import Any

from bro.llm import wires
from bro.trails.backends import (
  Classification,
  OpenedBody,
  ParsedRecord,
  TrailFormat,
  decoded_part_events,
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
  try:
    decoded = wires.decode(record.get('wire'), response)
  except wires.WireUnavailable:
    return [projected_event(record, 'harness_event', raw=record)]
  usage = record.get('usage')
  call_fields: dict[str, Any] = {
    'model': decoded.model,
    'usage': usage if isinstance(usage, dict) else decoded.usage,
  }
  if decoded.service_tier is not None:
    call_fields['service_tier'] = decoded.service_tier
  if decoded.stop != 'completed':
    call_fields['stop'] = decoded.stop
  return [projected_event(record, 'llm_call', **call_fields), *decoded_part_events(record, decoded)]


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
