"""Shared client-side write lifecycle for every trails recorder."""

import copy
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from bro.base.lulid import lulid
from bro.trails.model import StartRequest
from bro.trails.record.recording_map import RecordingMap, Span, TrailEntry
from bro.trails.store import TrailsStore

KEEPALIVE_INTERVAL_SECONDS = 60.0


class Recording:
  """one open trail with ordinal appends, extent validation, and liveness."""

  def __init__(self, store: TrailsStore, trail_id: str, extent: int):
    self.store = store
    self.trail_id = trail_id
    self._extent = extent
    self._last_write_monotonic = time.monotonic()
    self._ended = False
    self._lock = threading.RLock()

  @classmethod
  def create(cls, store: TrailsStore, request: StartRequest) -> 'Recording':
    opened = store.start_trail(request)
    return cls(store, opened['id'], opened['extent'])

  @property
  def extent(self) -> int:
    with self._lock:
      return self._extent

  def append(
    self,
    records: list[Any],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> int:
    """append one batch and return its starting ordinal."""
    with self._lock:
      if self._ended:
        raise RuntimeError('append called after recording ended')
      offset = self._extent
      expected_extent = offset + len(records)
      if tools is None:
        response = self.store.append_records(self.trail_id, offset, records)
      else:
        response = self.store.append_records(self.trail_id, offset, records, tools=tools)
      extent = response.get('extent')
      if not isinstance(extent, int) or isinstance(extent, bool) or extent != expected_extent:
        raise RuntimeError(
          f'append for trail {self.trail_id} returned extent {extent!r}, expected {expected_extent}'
        )
      self._extent = extent
      self._last_write_monotonic = time.monotonic()
      return offset

  def keepalive_if_idle(self) -> bool:
    """send a keepalive after one shared idle interval; return whether one sent."""
    with self._lock:
      if self._ended:
        return False
      if time.monotonic() - self._last_write_monotonic < KEEPALIVE_INTERVAL_SECONDS:
        return False
      self.store.keepalive(self.trail_id)
      self._last_write_monotonic = time.monotonic()
      return True

  def end(
    self,
    reason: str,
    detail: Optional[str] = None,
  ) -> None:
    """end this trail once."""
    with self._lock:
      if self._ended:
        return
      self.store.end_trail(self.trail_id, reason, detail)
      self._ended = True


@dataclass(frozen=True)
class MappedStart:
  """a persisted trail identity and the fixed request to send on every retry."""

  request: StartRequest

  @classmethod
  def prepare(
    cls,
    recording_map: RecordingMap,
    record_id: str,
    spans: tuple[Span, ...],
    request: StartRequest,
    *,
    closing: Optional[int] = None,
  ) -> 'MappedStart':
    """`closing` is the extent at which the open span of the record's last trail
    is closed, in the same map write as the new entry."""
    if request.id is not None or request.lineage is not None:
      raise ValueError('a mapped start requires an unnamed request without lineage')
    wire = copy.deepcopy(request.to_wire())
    wire['id'] = lulid()
    named = StartRequest.from_wire(wire)
    entry = TrailEntry(wire['id'], spans, forked_from=named.forked_from)
    entries = list(recording_map.records.get(record_id, []))
    if closing is not None:
      entries[-1] = entries[-1].close(closing)
    recording_map.set_entries(record_id, [*entries, entry])
    return cls(named)

  def start(self, store: TrailsStore) -> Recording:
    return Recording.create(store, self.request)
