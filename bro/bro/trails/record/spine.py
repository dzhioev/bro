"""Shared client-side write lifecycle for every trails recorder."""

import threading
import time
from typing import Any, Optional

from trails.client import RECORD_RETRY_DELAYS_SECONDS, TrailsClient

KEEPALIVE_INTERVAL_SECONDS = 60.0
WRITE_RETRY_DELAYS_SECONDS = RECORD_RETRY_DELAYS_SECONDS


class Recording:
  """one open trail with ordinal appends, extent validation, and liveness."""

  def __init__(self, client: TrailsClient, trail_id: str, extent: int):
    self.client = client
    self.trail_id = trail_id
    self._extent = extent
    self._last_write_monotonic = time.monotonic()
    self._ended = False
    self._lock = threading.RLock()

  @classmethod
  def create(cls, client: TrailsClient, payload: dict[str, Any]) -> 'Recording':
    records = payload.get('body', {}).get('records')
    if not isinstance(records, list):
      raise ValueError('trail body.records must be a list')
    trail_id: str = client.create_trail(payload)['id']
    return cls(client, trail_id, len(records))

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
        response = self.client.append_records(self.trail_id, offset, records)
      else:
        response = self.client.append_records(self.trail_id, offset, records, tools=tools)
      extent = response.get('extent')
      if not isinstance(extent, int) or isinstance(extent, bool) or extent != expected_extent:
        raise RuntimeError(
          f'append for trail {self.trail_id} returned extent {extent!r}, expected {expected_extent}'
        )
      self._extent = extent
      self._last_write_monotonic = time.monotonic()
      return offset

  def keepalive_if_idle(
    self,
    *,
    retry_delays: Optional[tuple[float, ...]] = None,
  ) -> bool:
    """send a keepalive after one shared idle interval; return whether one sent."""
    with self._lock:
      if self._ended:
        return False
      if time.monotonic() - self._last_write_monotonic < KEEPALIVE_INTERVAL_SECONDS:
        return False
      if retry_delays is None:
        self.client.keepalive(self.trail_id)
      else:
        self.client.keepalive(self.trail_id, retry_delays=retry_delays)
      self._last_write_monotonic = time.monotonic()
      return True

  def end(
    self,
    reason: str,
    detail: Optional[str] = None,
    *,
    retry_delays: Optional[tuple[float, ...]] = None,
  ) -> None:
    """end this trail once."""
    with self._lock:
      if self._ended:
        return
      if retry_delays is None:
        self.client.end_trail(self.trail_id, reason, detail)
      else:
        self.client.end_trail(self.trail_id, reason, detail, retry_delays=retry_delays)
      self._ended = True
