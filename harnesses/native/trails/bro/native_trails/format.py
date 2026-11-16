"""The bro trail format."""

from typing import Any

from bro.llm import wires
from bro.trails.backends import (
  Classification,
  OpenedBody,
  ParsedRecord,
  TrailFormat,
  decoded_part_events,
  parse_json_object,
  projected_event,
  validate_server_derived,
)
from bro.trails.model import is_sha256

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

BRO_TEXT_BODY_KINDS = frozenset({'system_prompt', 'user_input', 'notification'})


def _step_line(record: dict) -> dict:
  if type(record['run_log']) is not int or record['run_log'] != 1:
    raise ValueError(f'unsupported bro run_log format {record["run_log"]!r}')
  body = record.get('body')
  line = parse_json_object(body) if isinstance(body, str) else None
  if line is None:
    raise ValueError('bro line row body must be a JSON object string')
  kind = line.get('kind')
  if not isinstance(kind, str) or kind not in BRO_STEP_KINDS:
    raise ValueError(f'bro line kind must be one of {sorted(BRO_STEP_KINDS)}')
  if not isinstance(line.get('at'), str):
    raise ValueError('bro line at must be a string')
  if kind in BRO_TEXT_BODY_KINDS and not isinstance(line.get('body'), str):
    raise ValueError(f'bro {kind} body must be a string')
  if kind == 'llm_call' and not is_sha256(line.get('tools_sha256')):
    raise ValueError('bro llm_call tools_sha256 must be a SHA-256 digest')
  return line


def native_record(row: dict) -> dict:
  if 'run_log' not in row:
    return row
  line = _step_line(row)
  metadata = ('trail_id', 'format', 'step_id', 'ts', 'payload_sha256', 'usage')
  return {
    **{key: value for key, value in line.items() if key != 'at'},
    **{key: row[key] for key in metadata if key in row},
  }


def _bro_parse(payload: Any) -> ParsedRecord:
  if not isinstance(payload, dict):
    raise ValueError('bro record must be an object')
  if 'run_log' in payload:
    line = _step_line(payload)
    attributes = {'run_log': payload['run_log']}
    if line['kind'] == 'llm_call':
      attributes['tools_sha256'] = line['tools_sha256']
    return ParsedRecord(
      kind=line['kind'],
      body=payload['body'],
      timestamp=line['at'],
      attributes=attributes,
      native=dict(payload),
    )
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
  native = native_record(record.native)
  if native['kind'] == 'user_input':
    return Classification(turn_delta=1)
  body = native.get('body')
  if native['kind'] != 'llm_call' or not isinstance(body, dict):
    return Classification()
  response = body.get('response')
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
  record = native_record(record)
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
  native_record=native_record,
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
