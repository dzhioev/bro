"""Harness adapters for native records, aggregate classification, and projection."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from bro.trails import claude_lineage
from bro.trails.lineage import LineageDecision
from bro.trails.model import BlazeRequest

# the native header fields a store folds from a trail's rows
SERVER_DERIVED_NATIVE_FIELDS = frozenset({'usage', 'step_counts_by_kind', 'lineage_head'})

# the wire reason a blaze answers when the trail its verdict attaches to advanced
# between the verification and the write, leaving the awarded spans off by that
# much; the caller's next attempt resolves against the extent it reached
ATTACH_CONTENDED = 'the trail advanced while attaching'

# the native fields a mint settles for good: `segment` is the key the trail's
# index answers a lineage lookup on, so an attaching lifetime restamps its own
# facts over the header but never the identity that lookup found it by
_MINTED_NATIVE_FIELDS = frozenset({'segment'})

BRO_STEP_KINDS = frozenset(
  {
    'system_prompt',
    'user_input',
    'tool_result',
    'llm_call',
    'error',
  }
)

# the kinds whose body a reader is entitled to read as text: they project into a
# message whose content carries no other shape, so a body that is not a string
# leaves the trail unrenderable rather than merely odd
BRO_TEXT_BODY_KINDS = frozenset({'system_prompt', 'user_input'})


@dataclass(frozen=True)
class ParsedRecord:
  kind: Optional[str]
  body: Any
  timestamp: Optional[str]
  attributes: dict[str, Any]
  native: dict[str, Any]


@dataclass(frozen=True)
class Classification:
  turn_delta: int = 0
  usage_model: Optional[str] = None
  usage: Optional[dict] = None
  billing_key: Optional[str] = None
  native_updates: Optional[dict[str, Any]] = None
  subject: Optional[str] = None


@dataclass(frozen=True)
class OpenedBody:
  records: list[Any]


@dataclass(frozen=True)
class Adapter:
  parse: Callable[[Any], ParsedRecord]
  classify: Callable[[ParsedRecord], Classification]
  project: Callable[[dict], list[dict]]
  open: Callable[[dict], OpenedBody]
  validate_create: Callable[[dict], None]
  emitted_message_types: frozenset[str]
  # evidence, plus whatever row reads the store offers a resolver, to a verdict
  resolve_lineage: Optional[Callable[[dict, Any], LineageDecision]] = None


def resolve_lineage(adapter: Adapter, request: BlazeRequest, index: Any) -> LineageDecision:
  """The harness verdict for a blaze carrying lineage evidence."""
  assert request.lineage is not None
  if adapter.resolve_lineage is None:
    raise ValueError(f'the {request.harness} harness does not resolve lineage')
  return adapter.resolve_lineage(request.lineage, index)


def blaze_result(
  trail_id: str, started_at: str, extent: int, decision: Optional[LineageDecision]
) -> dict[str, Any]:
  """The blaze response: the recording trail's identity and the ordinal its
  writer appends from, plus the resolver's verdict when the request carried
  lineage evidence. An attached trail answers as itself, so its extent is
  whatever it already recorded."""
  result: dict[str, Any] = {'id': trail_id, 'started_at': started_at, 'extent': extent}
  if decision is not None:
    result['adopted'] = True
    result['chunks'] = decision.chunks
    if decision.attach_to is None:
      result['forked_from'] = decision.forked_from
    else:
      result['attached'] = True
    if decision.reason is not None:
      result['reason'] = decision.reason
  return result


def attached_header(header: dict, request: BlazeRequest) -> dict[str, Any]:
  """The header values a trail takes on when a lifetime attaches to it: the facts
  the blaze would have minted a trail with, latest-wins over the ones the previous
  lifetime left, and the end mark cleared so the trail is open again. `summoned_by`
  is left alone, since the attribution belongs to the run that opened the trail,
  and the server-derived native fold survives the merge because `validate_create`
  refuses a writer those fields."""
  restamped = {
    key: value for key, value in request.native.items() if key not in _MINTED_NATIVE_FIELDS
  }
  return {
    'end': None,
    'version': request.version,
    'hold': request.hold,
    'location': request.location,
    'native': {**header.get('native', {}), **restamped},
  }


def add_numeric_maps(left: dict, right: dict) -> dict:
  """Add numeric leaves while preserving the provider's raw usage vocabulary."""
  result = dict(left)
  for key, value in right.items():
    current = result.get(key)
    if isinstance(value, dict):
      result[key] = add_numeric_maps(current if isinstance(current, dict) else {}, value)
    elif isinstance(value, int) and not isinstance(value, bool):
      result[key] = int(current) + value if isinstance(current, int) else value
  return result


def _source(record: dict, index: int = 0) -> dict:
  return {'step_id': record['step_id'], 'index': index}


def _event(record: dict, event_type: str, index: int = 0, **fields: Any) -> dict:
  return {'type': event_type, 'ts': record.get('ts'), 'source': _source(record, index), **fields}


def _parse_json_object(raw: str) -> Optional[dict]:
  try:
    value = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return value if isinstance(value, dict) else None


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
  records = body.get('records')
  if not isinstance(records, list):
    raise ValueError('bro body.records must be a list')
  return OpenedBody(records=records)


def _validate_server_derived(native: dict) -> None:
  sent = SERVER_DERIVED_NATIVE_FIELDS & set(native)
  if len(sent) > 0:
    raise ValueError(f'native {", ".join(sorted(sent))} are server-derived')


def _bro_validate_create(native: dict) -> None:
  _validate_server_derived(native)
  if not isinstance(native.get('llm'), dict):
    raise ValueError('native.llm is required for the bro harness')


def _bro_llm_call_messages(record: dict) -> list[dict]:
  body = record.get('body')
  response = body.get('response') if isinstance(body, dict) else None
  if not isinstance(response, dict):
    return [_event(record, 'harness_event', raw=record)]
  usage = record.get('usage')
  if not isinstance(usage, dict):
    raw_usage = response.get('usage')
    usage = raw_usage if isinstance(raw_usage, dict) else {}
  events = [_event(record, 'llm_call', model=str(response.get('model', 'unknown')), usage=usage)]
  output = response.get('output')
  if not isinstance(output, list):
    return events
  terminal = not any(
    isinstance(item, dict) and item.get('type') == 'function_call' for item in output
  )
  for index, item in enumerate(output, start=1):
    if not isinstance(item, dict):
      events.append(_event(record, 'harness_event', index, raw=item))
      continue
    item_type = item.get('type')
    if item_type == 'reasoning':
      summary = item.get('summary')
      if not isinstance(summary, list):
        events.append(_event(record, 'harness_event', index, raw=item))
        continue
      for part in summary:
        if isinstance(part, dict) and part.get('type') == 'summary_text':
          text = part.get('text')
          if isinstance(text, str) and len(text) > 0:
            events.append(_event(record, 'reasoning', index, content=text))
    elif item_type == 'message':
      content = item.get('content')
      if not isinstance(content, list):
        events.append(_event(record, 'harness_event', index, raw=item))
        continue
      text = ''.join(
        part.get('text', '')
        for part in content
        if isinstance(part, dict)
        and part.get('type') == 'output_text'
        and isinstance(part.get('text'), str)
      )
      if len(text) > 0:
        events.append(_event(record, 'assistant', index, content=text, terminal=terminal))
    elif item_type == 'function_call':
      raw_arguments = item.get('arguments')
      try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
      except json.JSONDecodeError:
        arguments = {'_raw_arguments': raw_arguments}
      events.append(
        _event(
          record,
          'tool_call',
          index,
          tool_name=item.get('name'),
          call_id=item.get('call_id'),
          arguments=arguments,
        )
      )
    else:
      events.append(_event(record, 'harness_event', index, raw=item))
  return events


def _bro_project(record: dict) -> list[dict]:
  kind = record.get('kind')
  if kind == 'llm_call':
    return _bro_llm_call_messages(record)
  if kind in {'system_prompt', 'user_input', 'tool_result', 'error'}:
    fields: dict[str, Any] = {'content': record.get('body')}
    for key in ('tool_name', 'arguments', 'call_id', 'is_error'):
      if key in record:
        fields[key] = record[key]
    return [_event(record, kind, **fields)]
  return [_event(record, 'harness_event', raw=record)]


def _claude_parse(payload: Any) -> ParsedRecord:
  raw = payload.get('body') if isinstance(payload, dict) else payload
  if not isinstance(raw, str):
    raise ValueError('claude record must be a raw JSONL string')
  record = _parse_json_object(raw)
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
  records = body.get('records')
  if not isinstance(records, list) or not all(isinstance(record, str) for record in records):
    raise ValueError('claude body.records must be a list of strings')
  return OpenedBody(records=records)


def _claude_validate_create(native: dict) -> None:
  _validate_server_derived(native)
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
    return [_event(record, 'error', content=message.get('content'))]
  model = str(message.get('model', 'unknown'))
  raw_usage = message.get('usage')
  if not isinstance(raw_usage, dict) or model == '<synthetic>':
    return [_event(record, 'harness_event', raw=native)]
  events: list[dict] = []
  contributed_usage = record.get('usage')
  if isinstance(contributed_usage, dict):
    events.append(_event(record, 'llm_call', model=model, usage=contributed_usage))
  content = message.get('content')
  if not isinstance(content, list):
    return [*events, _event(record, 'harness_event', 1, raw=native)]
  for index, block in enumerate(content, start=1):
    if not isinstance(block, dict):
      events.append(_event(record, 'harness_event', index, raw=block))
    elif block.get('type') == 'thinking':
      events.append(_event(record, 'reasoning', index, content=block.get('thinking')))
    elif block.get('type') == 'text':
      events.append(_event(record, 'assistant', index, content=block.get('text')))
    elif block.get('type') == 'tool_use':
      events.append(
        _event(
          record,
          'tool_call',
          index,
          tool_name=block.get('name'),
          call_id=block.get('id'),
          arguments=block.get('input'),
        )
      )
    else:
      events.append(_event(record, 'harness_event', index, raw=block))
  return events


def _claude_user_messages(record: dict, native: dict, message: dict) -> list[dict]:
  content = message.get('content')
  if _claude_tool_results_only(message):
    assert isinstance(content, list)
    return [
      _event(
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
  return [
    _event(
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


def _claude_project(record: dict) -> list[dict]:
  native = _claude_parse(record).native['record']
  if not isinstance(native, dict):
    return [_event(record, 'harness_event', raw=record.get('body'))]
  message = native.get('message')
  if native.get('type') == 'assistant' and isinstance(message, dict):
    return _claude_assistant_messages(record, native, message)
  if native.get('type') == 'user' and isinstance(message, dict):
    return _claude_user_messages(record, native, message)
  return [_event(record, 'harness_event', raw=native)]


BRO_ADAPTER = Adapter(
  parse=_bro_parse,
  classify=_bro_classify,
  project=_bro_project,
  open=_bro_open,
  validate_create=_bro_validate_create,
  emitted_message_types=frozenset(
    {
      'user_input',
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
)

CLAUDE_ADAPTER = Adapter(
  parse=_claude_parse,
  classify=_claude_classify,
  project=_claude_project,
  open=_claude_open,
  validate_create=_claude_validate_create,
  resolve_lineage=claude_lineage.resolve,
  emitted_message_types=frozenset(
    {
      'user_input',
      'llm_call',
      'reasoning',
      'assistant',
      'tool_call',
      'tool_result',
      'error',
      'harness_event',
    }
  ),
)

BACKENDS = {'bro': BRO_ADAPTER, 'claude': CLAUDE_ADAPTER}
