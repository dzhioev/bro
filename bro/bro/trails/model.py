"""Shared trail records and wire vocabulary."""

from dataclasses import dataclass
from typing import Any, Optional, TypedDict, cast


@dataclass(frozen=True)
class ForkedFrom:
  """Pointer to a source trail's fork point."""

  trail_id: str
  step_id: str


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
  step_id: str
  ts: str
  kind: str
  body: Any
  extras: dict[str, Any]


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


def spill_descriptor(value: Any) -> Optional[SpillDescriptor]:
  """Return the exact spill descriptor shape, or ``None`` for an inline body."""
  if isinstance(value, dict) and frozenset(value.keys()) == _SPILL_DESCRIPTOR_KEYS:
    return cast(SpillDescriptor, value)
  return None
