"""settle local recording entries with only the store's record permission."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional

from bro.base import log
from bro.trails.record.recording_map import RecordingMap, RecordingMapError, TrailEntry
from bro.trails.store import AppendConflict, TrailNotFound, TrailsStore


def trail_extent(store: TrailsStore, trail_id: str) -> int:
  """probe the row count without reading a trail."""
  try:
    response = store.append_records(trail_id, 0, [])
  except AppendConflict as conflict:
    extent = conflict.actual
  else:
    extent = response['extent']
  if not isinstance(extent, int) or isinstance(extent, bool) or extent < 0:
    raise RuntimeError(f'extent probe for trail {trail_id} returned invalid extent {extent!r}')
  return extent


@dataclass(frozen=True)
class SettledTrail:
  entry: TrailEntry
  extent: int


def settle_record(
  recording_map: RecordingMap,
  record_id: str,
  store: TrailsStore,
  records: Sequence[Any],
) -> Optional[SettledTrail]:
  """verify the newest standing entry against the record's append payloads."""
  entries = recording_map.records.get(record_id)
  if entries is None:
    return None
  if any(entry.restored for entry in entries):
    raise RecordingMapError('restored entries are not supported by the recorder')
  while len(entries) > 0:
    entry = entries[-1]
    try:
      extent = trail_extent(store, entry.id)
    except TrailNotFound:
      extent = 0
    if extent == 0:
      _discard(recording_map, record_id, entries[-1:])
      entries = entries[:-1]
      continue
    try:
      item = entry.item_at(extent - 1)
    except IndexError:
      _discard(recording_map, record_id, entries[:])
      return None
    if item >= len(records):
      _discard(recording_map, record_id, entries[:])
      return None
    try:
      response = store.append_records(entry.id, extent - 1, [records[item]])
    except AppendConflict:
      _discard(recording_map, record_id, entries[:])
      return None
    if response.get('duplicate') is not True or response.get('extent') != extent:
      raise RuntimeError(
        f'verification for trail {entry.id} returned no duplicate at extent {extent}'
      )
    return SettledTrail(entry, extent)
  return None


def _discard(recording_map: RecordingMap, record_id: str, removed: list[TrailEntry]) -> None:
  entries = recording_map.records[record_id]
  remaining = entries[: -len(removed)]
  recording_map.set_entries(record_id, remaining)
  for entry in removed:
    log.warning('gave up trail %s while settling record %s', entry.id, record_id)
