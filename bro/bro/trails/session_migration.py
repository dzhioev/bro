"""Pure planning primitives for the legacy Claude session-log migration."""

import dataclasses
import datetime
import hashlib
import json
import shlex
from decimal import Decimal
from typing import Any, Optional

from trails.server.backends import add_numeric_maps, claude_artifact_key, claude_context_key

EVENT_TYPE = 'cw-conversation-event'
LEGACY_VERSION = 'legacy-session-log'
RAISE_TOOL = 'mcp__bro__raise'

_CROCKFORD = '0123456789abcdefghjkmnpqrstvwxyz'


@dataclasses.dataclass(frozen=True)
class Source:
  identity: str
  key: str
  body: bytes
  modified_at: str
  table_item: Optional[dict]
  duplicate_keys: tuple[str, ...] = ()

  @property
  def orphan(self) -> bool:
    return self.table_item is None


@dataclasses.dataclass
class Split:
  body: bytes
  segment: str
  started_at: str
  end_at: str
  end_reason: str
  end_detail: Optional[str]
  verified_from_previous: bool
  trustworthy_end: bool
  scan: 'Scan'


@dataclasses.dataclass(frozen=True)
class PlannedTrail:
  header: dict
  artifact: bytes
  context: Optional[bytes]


@dataclasses.dataclass(frozen=True)
class SourcePlan:
  source: Source
  trails: tuple[PlannedTrail, ...]
  marker_bytes: int
  marker_lines: int
  degenerate: bool


class Scan:
  def __init__(self) -> None:
    self.started_at: Optional[str] = None
    self.harness_version: Optional[str] = None
    self.subject: Optional[str] = None
    self.ai_title: Optional[str] = None
    self.usage: dict[str, dict] = {}
    self.turn_count = 0
    self.raised: Optional[str] = None
    self.segments: list[str] = []
    self._billed_message_ids: set[str] = set()

  @staticmethod
  def _content_text(record: dict) -> Optional[str]:
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
      return content
    if isinstance(content, list):
      texts = [
        block.get('text', '')
        for block in content
        if isinstance(block, dict) and block.get('type') == 'text'
      ]
      joined = '\n'.join(texts)
      return joined if len(joined) > 0 else None
    return None

  def feed(self, record: dict) -> None:
    if self.started_at is None and isinstance(record.get('timestamp'), str):
      self.started_at = record['timestamp']
    if self.harness_version is None and isinstance(record.get('version'), str):
      self.harness_version = record['version']
    segment = record.get('sessionId')
    if isinstance(segment, str) and segment not in self.segments:
      self.segments.append(segment)
    if record.get('type') == 'ai-title':
      title = record.get('aiTitle')
      if isinstance(title, str) and len(title) > 0:
        self.ai_title = title
    if record.get('type') == 'assistant':
      self._feed_assistant(record)
    elif record.get('type') == 'user':
      self._feed_user(record)

  def _feed_assistant(self, record: dict) -> None:
    message = record.get('message')
    if not isinstance(message, dict):
      return
    usage = message.get('usage')
    model = str(message.get('model', 'unknown'))
    if (
      isinstance(usage, dict)
      and model != '<synthetic>'
      and record.get('isApiErrorMessage') is not True
    ):
      message_id = message.get('id')
      if not isinstance(message_id, str) or message_id not in self._billed_message_ids:
        if isinstance(message_id, str):
          self._billed_message_ids.add(message_id)
        self.usage[model] = add_numeric_maps(self.usage.get(model, {}), usage)
    content = message.get('content')
    if isinstance(content, list):
      for block in content:
        if (
          isinstance(block, dict)
          and block.get('type') == 'tool_use'
          and block.get('name') == RAISE_TOOL
        ):
          reason = block.get('input', {}).get('reason')
          self.raised = reason if isinstance(reason, str) else ''

  def _feed_user(self, record: dict) -> None:
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    tool_results_only = isinstance(content, list) and all(
      isinstance(block, dict) and block.get('type') == 'tool_result' for block in content
    )
    text = self._content_text(record)
    if record.get('isMeta') is not True and not tool_results_only:
      self.turn_count += 1
      if self.subject is None and record.get('isSidechain') is not True and text is not None:
        stripped = text.lstrip()
        if not stripped.startswith('<'):
          first_line = stripped.split('\n', 1)[0].strip()
          if len(first_line) > 0:
            self.subject = first_line
    if self.raised is not None and text is not None:
      self.raised = None

  @classmethod
  def from_lines(cls, lines: list[bytes]) -> 'Scan':
    scan = cls()
    for raw in lines:
      record = parse_record(raw)
      if record is not None:
        scan.feed(record)
    return scan


def parse_record(raw: bytes) -> Optional[dict]:
  try:
    value = json.loads(raw)
  except (UnicodeDecodeError, json.JSONDecodeError):
    return None
  return value if isinstance(value, dict) else None


def _timestamp(record: Optional[dict]) -> Optional[str]:
  if record is None:
    return None
  value = record.get('timestamp')
  return value if isinstance(value, str) else None


def _segment(record: Optional[dict]) -> Optional[str]:
  if record is None:
    return None
  value = record.get('sessionId')
  return value if isinstance(value, str) else None


def _fallback_segment(source: Source) -> str:
  if source.table_item is not None:
    raw = source.table_item.get('segments')
    if isinstance(raw, str):
      try:
        segments = json.loads(raw)
      except json.JSONDecodeError:
        segments = None
      if isinstance(segments, list) and len(segments) > 0 and isinstance(segments[-1], str):
        return segments[-1]
  return source.identity


def _fallback_start(source: Source) -> str:
  if source.table_item is not None and isinstance(source.table_item.get('started_at'), str):
    return source.table_item['started_at']
  return source.modified_at


def _source_end(source: Source) -> str:
  if source.table_item is not None and isinstance(source.table_item.get('synced_at'), str):
    return source.table_item['synced_at']
  return source.modified_at


def _normal_lines(body: bytes) -> list[bytes]:
  return body.splitlines(keepends=True)


def split_source(source: Source) -> tuple[list[Split], int, int, bool]:
  lines = _normal_lines(source.body)
  parsed = [parse_record(line) for line in lines]
  if any(
    record is not None
    and record.get('type') == EVENT_TYPE
    and record.get('subtype') == 'missing-segment'
    for record in parsed
  ):
    scan = Scan.from_lines(lines)
    split = Split(
      body=source.body,
      segment=scan.segments[-1] if len(scan.segments) > 0 else _fallback_segment(source),
      started_at=scan.started_at if scan.started_at is not None else _fallback_start(source),
      end_at=_source_end(source),
      end_reason='ok' if source.table_item is not None else 'lost',
      end_detail=None,
      verified_from_previous=False,
      trustworthy_end=False,
      scan=scan,
    )
    return [split], 0, 0, True

  split_specs: list[tuple[list[bytes], Optional[str], bool, bool]] = []
  current: list[bytes] = []
  current_segment: Optional[str] = None
  pending_verified = False
  marker_bytes = 0
  marker_lines = 0

  def flush(end_at: Optional[str], trustworthy: bool) -> None:
    nonlocal current, current_segment, pending_verified
    if len(current) == 0:
      return
    split_specs.append((current, end_at, pending_verified, trustworthy))
    current = []
    current_segment = None
    pending_verified = False

  for raw, record in zip(lines, parsed, strict=True):
    if record is not None and record.get('type') == EVENT_TYPE:
      marker_bytes += len(raw)
      marker_lines += 1
      subtype = record.get('subtype')
      timestamp = _timestamp(record)
      if subtype == 'leave':
        flush(timestamp, True)
      elif subtype == 'resume':
        flush(timestamp, record.get('historyVerified') is True)
        pending_verified = record.get('historyVerified') is True
      else:
        flush(timestamp, False)
      continue
    segment = _segment(record)
    if (
      len(current) > 0
      and segment is not None
      and current_segment is not None
      and segment != current_segment
    ):
      flush(_timestamp(record), False)
    current.append(raw)
    if current_segment is None and segment is not None:
      current_segment = segment
  flush(None, False)

  if len(split_specs) == 0:
    scan = Scan.from_lines(lines)
    split_specs = [(lines, None, False, False)]
    marker_bytes = 0
    marker_lines = 0
    degenerate = True
  else:
    degenerate = False

  splits: list[Split] = []
  for index, (part_lines, boundary_at, verified, trustworthy) in enumerate(split_specs):
    scan = Scan.from_lines(part_lines)
    final = index == len(split_specs) - 1
    if final:
      end_at = _source_end(source)
      raised = None
      if source.table_item is not None and isinstance(source.table_item.get('raised'), str):
        raised = source.table_item['raised']
      elif scan.raised is not None:
        raised = scan.raised
      if raised is not None:
        end_reason = 'raised'
        end_detail = raised if len(raised) > 0 else 'raise reason unavailable'
      elif source.table_item is not None or trustworthy:
        end_reason = 'ok'
        end_detail = None
      else:
        end_reason = 'lost'
        end_detail = None
    else:
      end_at = boundary_at if boundary_at is not None else _source_end(source)
      end_reason = 'ok' if trustworthy else 'lost'
      end_detail = None
    segment = scan.segments[-1] if len(scan.segments) > 0 else _fallback_segment(source)
    splits.append(
      Split(
        body=b''.join(part_lines),
        segment=segment,
        started_at=scan.started_at if scan.started_at is not None else _fallback_start(source),
        end_at=end_at,
        end_reason=end_reason,
        end_detail=end_detail,
        verified_from_previous=verified and index > 0,
        trustworthy_end=trustworthy,
        scan=scan,
      )
    )
  return splits, marker_bytes, marker_lines, degenerate


def _encode_base32(value: int, width: int) -> str:
  chars = []
  for _ in range(width):
    chars.append(_CROCKFORD[value & 31])
    value >>= 5
  return ''.join(reversed(chars))


def deterministic_lulid(identity: str, ordinal: int, started_at: str) -> str:
  try:
    moment = datetime.datetime.fromisoformat(started_at.replace('Z', '+00:00'))
  except ValueError as exception:
    raise ValueError(f'invalid split timestamp {started_at!r}') from exception
  milliseconds = int(moment.timestamp() * 1000)
  if milliseconds < 0 or milliseconds >= 2**48:
    raise ValueError(f'split timestamp is outside the ULID range: {started_at!r}')
  entropy = int.from_bytes(hashlib.sha256(f'{identity}\0{ordinal}'.encode()).digest()[:10], 'big')
  raw = _encode_base32(milliseconds, 10) + _encode_base32(entropy, 16)
  return f'{raw[:10]}-{raw[10:18]}-{raw[18:]}'


def _command_option(command: str, name: str) -> Optional[str]:
  try:
    arguments = shlex.split(command)
  except ValueError:
    return None
  option = f'--{name}'
  for index, argument in enumerate(arguments):
    if argument.startswith(option + '='):
      return argument.split('=', 1)[1]
    if argument == option and index + 1 < len(arguments):
      return arguments[index + 1]
  return None


def _location(item: Optional[dict]) -> Optional[dict]:
  if item is None:
    return None
  location: dict[str, Any] = {}
  workspace = item.get('workspace')
  if isinstance(workspace, str):
    location['workspace'] = workspace
  is_container = item.get('is_container')
  if isinstance(is_container, bool):
    location['is_container'] = is_container
  host = item.get('host')
  if is_container is False and isinstance(host, str):
    location['host'] = host
  return location if len(location) > 0 else None


def _context(item: Optional[dict]) -> Optional[bytes]:
  if item is None or not isinstance(item.get('context'), str):
    return None
  raw = item['context']
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as exception:
    raise ValueError('legacy launch context is not valid JSON') from exception
  return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def _launch_native(item: Optional[dict]) -> dict:
  if item is None:
    return {}
  native: dict[str, Any] = {}
  model = item.get('model')
  native['llm'] = {'model': model} if isinstance(model, str) else {}
  command = item.get('cw_command')
  if isinstance(command, str):
    native['cw_command'] = command
  return native


def _subject(scan: Scan, item: Optional[dict], final: bool) -> Optional[str]:
  if scan.ai_title is not None:
    return scan.ai_title
  if scan.subject is not None:
    return scan.subject
  if final and item is not None and isinstance(item.get('subject'), str):
    return item['subject']
  return None


def _trail_ids(source: Source, splits: list[Split]) -> list[str]:
  ids = [
    deterministic_lulid(source.identity, index, split.started_at)
    for index, split in enumerate(splits)
  ]
  if source.orphan:
    ids[0] = source.identity
  else:
    ids[-1] = source.identity
  if len(ids) != len(set(ids)):
    raise ValueError(f'deterministic trail id collision in source {source.key}')
  return ids


def plan_source(source: Source) -> SourcePlan:
  splits, marker_bytes, marker_lines, degenerate = split_source(source)
  ids = _trail_ids(source, splits)
  trails: list[PlannedTrail] = []
  item = source.table_item
  location = _location(item)
  command = item.get('cw_command') if item is not None else None
  bro = _command_option(command, 'bro') if isinstance(command, str) else None
  context = _context(item)
  for index, (trail_id, split) in enumerate(zip(ids, splits, strict=True)):
    final = index == len(splits) - 1
    native: dict[str, Any] = {
      'segment': split.segment,
      's3_key': claude_artifact_key(trail_id),
      'harness_version': split.scan.harness_version
      if split.scan.harness_version is not None
      else (
        item['claude_code_version']
        if item is not None and isinstance(item.get('claude_code_version'), str)
        else 'unknown'
      ),
      'line_count': len(split.body.splitlines()),
      'size_bytes': len(split.body),
      'usage': split.scan.usage,
    }
    if final:
      native.update(_launch_native(item))
      if context is not None:
        native['context_s3'] = claude_context_key(trail_id)
    header: dict[str, Any] = {
      'id': trail_id,
      'harness': 'claude',
      'version': LEGACY_VERSION,
      'started_at': split.started_at,
      'end': {
        'at': split.end_at,
        'reason': split.end_reason,
        **({'detail': split.end_detail} if split.end_detail is not None else {}),
      },
      'last_alive_at': split.end_at,
      'interactive': True,
      'surface': 'cw',
      'turn_count': split.scan.turn_count,
      'native': native,
      'gsi_pk': 'trail',
    }
    if location is not None:
      header['location'] = location
    if bro is not None:
      header['bro'] = bro
    subject = _subject(split.scan, item, final)
    if subject is not None:
      header['subject'] = subject
    if split.verified_from_previous:
      parent_id = ids[index - 1]
      parent_lines = len(splits[index - 1].body.splitlines())
      if parent_lines == 0:
        split.verified_from_previous = False
      else:
        header['forked_from'] = {'trail_id': parent_id, 'step_id': str(parent_lines - 1)}
        header['forked_from_id'] = parent_id
    trails.append(
      PlannedTrail(header=header, artifact=split.body, context=context if final else None)
    )
  covered = sum(len(trail.artifact) for trail in trails) + marker_bytes
  if covered != len(source.body):
    raise ValueError(
      f'source byte coverage mismatch for {source.key}: {covered} != {len(source.body)}'
    )
  return SourcePlan(
    source=source,
    trails=tuple(trails),
    marker_bytes=marker_bytes,
    marker_lines=marker_lines,
    degenerate=degenerate,
  )


def normalise_decimal(value: Any) -> Any:
  if isinstance(value, Decimal):
    if value % 1 != 0:
      raise ValueError(f'non-integral DynamoDB number: {value}')
    return int(value)
  if isinstance(value, list):
    return [normalise_decimal(item) for item in value]
  if isinstance(value, dict):
    return {key: normalise_decimal(item) for key, item in value.items()}
  return value


def sha256(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def manifest_source(plan: SourcePlan) -> dict:
  return {
    'identity': plan.source.identity,
    'source_key': plan.source.key,
    'source_size_bytes': len(plan.source.body),
    'source_sha256': sha256(plan.source.body),
    'orphan': plan.source.orphan,
    'duplicate_keys': list(plan.source.duplicate_keys),
    'marker_bytes': plan.marker_bytes,
    'marker_lines': plan.marker_lines,
    'degenerate': plan.degenerate,
    'trails': [
      {
        'id': trail.header['id'],
        'artifact_size_bytes': len(trail.artifact),
        'artifact_sha256': sha256(trail.artifact),
        'header': trail.header,
        'context_sha256': sha256(trail.context) if trail.context is not None else None,
      }
      for trail in plan.trails
    ],
  }
