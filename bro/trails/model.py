"""Shared trail records and wire vocabulary."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, TypedDict, cast

UUID_LOOKUP_LIMIT = 100
UNREPORTED_END_INFERENCE = 'unreported'

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
  """A body stored outside its trail row and exposed through a presigned URL."""

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


def spill_descriptor(value: Any) -> Optional[SpillDescriptor]:
  """Return the exact spill descriptor shape, or ``None`` for an inline body."""
  if isinstance(value, dict) and frozenset(value.keys()) == _SPILL_DESCRIPTOR_KEYS:
    return cast(SpillDescriptor, value)
  return None
