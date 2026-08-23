"""Shared trail records and wire vocabulary."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, TypedDict, cast

UNREPORTED_END_INFERENCE = 'unreported'
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
VALID_END_REASONS = frozenset({'ok', 'raised', 'error'})
VALID_HOLDS = frozenset({'guided', 'attended', 'detached', 'unattended'})

MESSAGE_TYPES = frozenset(
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
)


@dataclass(frozen=True)
class BlazeRequest:
  harness: str
  version: str
  interactive: bool
  surface: str
  body: dict[str, Any]
  native: dict[str, Any]
  bro: Optional[str] = None
  hold: Optional[str] = None
  forked_from: Optional[dict[str, Any]] = None
  summoned_by: Optional[dict[str, Any]] = None
  subject: Optional[str] = None
  location: Optional[dict[str, Any]] = None
  lineage: Optional[dict[str, Any]] = None
  """harness-specific evidence for the trail's lineage, interpreted by the
  harness adapter's resolver."""

  def __post_init__(self) -> None:
    for field in ('harness', 'version', 'surface'):
      value = getattr(self, field)
      if not isinstance(value, str) or len(value) == 0:
        raise ValueError(f'{field} must be a non-empty string')
    if not isinstance(self.interactive, bool):
      raise ValueError('interactive must be a bool')
    if not isinstance(self.body, dict):
      raise ValueError('body must be an object')
    if not isinstance(self.native, dict):
      raise ValueError('native must be an object')
    _validate_lineage(self.lineage)
    if self.lineage is not None and self.forked_from is not None:
      raise ValueError('lineage evidence and forked_from are mutually exclusive')
    for field in ('bro', 'subject'):
      value = getattr(self, field)
      if value is not None and (not isinstance(value, str) or len(value) == 0):
        raise ValueError(f'{field} must be a non-empty string')
    if self.hold is not None and self.hold not in VALID_HOLDS:
      raise ValueError(f'hold must be one of {sorted(VALID_HOLDS)}')
    _validate_pointer(self.forked_from, 'forked_from', step_optional=False)
    _validate_pointer(self.summoned_by, 'summoned_by', step_optional=True)
    _validate_location(self.location)

  @classmethod
  def from_wire(cls, data: dict[str, Any]) -> 'BlazeRequest':
    if not isinstance(data, dict):
      raise ValueError('blaze request must be an object')
    fields = {
      'harness',
      'version',
      'interactive',
      'surface',
      'body',
      'native',
      'bro',
      'hold',
      'forked_from',
      'summoned_by',
      'subject',
      'location',
      'lineage',
    }
    unknown = set(data) - fields
    if len(unknown) > 0:
      raise ValueError(f'unknown fields: {sorted(unknown)}')
    required = {'harness', 'version', 'interactive', 'surface', 'body', 'native'}
    missing = required - set(data)
    if len(missing) > 0:
      raise ValueError(f'missing fields: {sorted(missing)}')
    return cls(
      harness=data['harness'],
      version=data['version'],
      interactive=data['interactive'],
      surface=data['surface'],
      body=data['body'],
      native=data['native'],
      bro=data.get('bro'),
      hold=data.get('hold'),
      forked_from=data.get('forked_from'),
      summoned_by=data.get('summoned_by'),
      subject=data.get('subject'),
      location=data.get('location'),
      lineage=data.get('lineage'),
    )

  def to_wire(self) -> dict[str, Any]:
    data: dict[str, Any] = {
      'harness': self.harness,
      'version': self.version,
      'interactive': self.interactive,
      'surface': self.surface,
      'body': self.body,
      'native': self.native,
    }
    for field in ('bro', 'hold', 'forked_from', 'summoned_by', 'subject', 'location', 'lineage'):
      value = getattr(self, field)
      if value is not None:
        data[field] = value
    return data


def validate_end(reason: Any, detail: Any) -> tuple[str, Optional[str]]:
  if not isinstance(reason, str) or reason not in VALID_END_REASONS:
    raise ValueError(f'reason must be one of {sorted(VALID_END_REASONS)}')
  if detail is not None and not isinstance(detail, str):
    raise ValueError('detail must be a string or null')
  if reason in {'raised', 'error'} and (detail is None or len(detail) == 0):
    raise ValueError(f'detail is required for {reason}')
  return reason, detail


_MISSING_TRAIL_FIELD = 'missing_trail'


def trail_not_found_body(trail_id: str) -> dict[str, Any]:
  """the body a trails server answers a request for a missing trail with. the
  `missing_trail` field is what separates it from every other 404 a client can
  receive — an unrouted path, or one from an intermediary."""
  return {'error': f'trail not found: {trail_id}', _MISSING_TRAIL_FIELD: trail_id}


def reported_missing_trail(raw: bytes) -> Optional[str]:
  """the trail id a 404 response body reports missing, or None when the body
  reports anything else."""
  try:
    body = json.loads(raw)
  except (json.JSONDecodeError, UnicodeDecodeError):
    return None
  if not isinstance(body, dict):
    return None
  trail_id = body.get(_MISSING_TRAIL_FIELD)
  return trail_id if isinstance(trail_id, str) else None


_FORKS_FIELD = 'forks'


def trail_has_forks_body(message: str, forks: list[str]) -> dict[str, Any]:
  """the body a trails server refuses to remove a forked trail with; `forks`
  names the trails whose lineage still points at it."""
  return {'error': message, _FORKS_FIELD: forks}


def reported_forks(raw: bytes) -> Optional[list[str]]:
  """the forks a 409 response body blames the refusal on, or None for a 409 from
  any other conditional write."""
  try:
    body = json.loads(raw)
  except (json.JSONDecodeError, UnicodeDecodeError):
    return None
  if not isinstance(body, dict):
    return None
  forks = body.get(_FORKS_FIELD)
  if not isinstance(forks, list) or not all(isinstance(fork, str) for fork in forks):
    return None
  return forks


def _validate_pointer(value: Any, field: str, *, step_optional: bool) -> None:
  if value is None:
    return
  allowed = {'trail_id', 'step_id', 'index'}
  required = {'trail_id'} if step_optional else {'trail_id', 'step_id'}
  if (
    not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(allowed)
  ):
    raise ValueError(f'{field} has an invalid pointer shape')
  if not isinstance(value['trail_id'], str) or len(value['trail_id']) == 0:
    raise ValueError(f'{field}.trail_id must be a non-empty string')
  for ordinal in ('step_id', 'index'):
    item = value.get(ordinal)
    if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item < 0):
      raise ValueError(f'{field}.{ordinal} must be a non-negative int')


def _validate_lineage(value: Any) -> None:
  if value is not None and not isinstance(value, dict):
    raise ValueError('lineage must be an object')


def _validate_location(value: Any) -> None:
  if value is None:
    return
  if not isinstance(value, dict) or not set(value).issubset(
    {'host', 'workspace', 'dir', 'is_container'}
  ):
    raise ValueError('location has unknown fields')
  for field in ('host', 'workspace', 'dir'):
    if value.get(field) is not None and not isinstance(value[field], str):
      raise ValueError(f'location.{field} must be a string')
  if value.get('is_container') is not None and not isinstance(value['is_container'], bool):
    raise ValueError('location.is_container must be a bool')


@dataclass(frozen=True)
class ForkedFrom:
  """Pointer to a source trail's fork point."""

  trail_id: str
  step_id: int
  index: Optional[int] = None


@dataclass(frozen=True)
class Trail:
  """Trail header metadata."""

  id: str
  harness: str
  bro: Optional[str]
  version: str
  native: dict
  started_at: str
  interactive: bool
  surface: str
  forked_from: Optional[ForkedFrom]
  summoned_by: Optional[dict[str, Any]] = None
  hold: Optional[str] = None

  @property
  def llm_spec(self) -> dict:
    return self.native['llm']


@dataclass(frozen=True)
class Step:
  """One harness-native record in a trail."""

  trail_id: str
  step_id: int
  ts: Optional[str]
  kind: Optional[str]
  body: Any
  extras: dict[str, Any]
  usage: Optional[dict] = None


@dataclass(frozen=True)
class RecordedTrail:
  """A trail header with its ordered native records."""

  header: Trail
  steps: list[Step]


class SpillDescriptor(TypedDict):
  """An external body reference accepted by store body resolution."""

  s3: str
  url: str
  size: int


_SPILL_DESCRIPTOR_KEYS = frozenset(SpillDescriptor.__required_keys__)


def canonical_json_bytes(value: Any) -> bytes:
  return json.dumps(
    value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
  ).encode('utf-8')


def tools_sha256(tools: Any) -> str:
  return hashlib.sha256(canonical_json_bytes(tools)).hexdigest()


def payload_sha256(payload: Any) -> str:
  """the digest a stored row carries as `payload_sha256`."""
  return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def spill_descriptor(value: Any) -> Optional[SpillDescriptor]:
  """Return the exact spill descriptor shape, or ``None`` for an inline body."""
  if isinstance(value, dict) and frozenset(value.keys()) == _SPILL_DESCRIPTOR_KEYS:
    return cast(SpillDescriptor, value)
  return None
