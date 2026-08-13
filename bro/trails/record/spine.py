"""Shared client-side write lifecycle for every trails recorder."""

import threading
import time
from typing import Any, Optional

from bro.trails.model import BlazeRequest
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
  def create(cls, store: TrailsStore, request: BlazeRequest) -> 'Recording':
    records = request.body.get('records')
    if not isinstance(records, list):
      raise ValueError('trail body.records must be a list')
    trail_id: str = store.blaze(request)['id']
    return cls(store, trail_id, len(records))

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
