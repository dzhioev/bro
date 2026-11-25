"""local records and the trail spans that record them."""

import json
import os
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from bro.base.lulid import is_lulid

FILENAME = 'recording-map.json'


class RecordingMapError(ValueError):
  """a recording map the recorder must refuse permanently."""


def _ordinal(value: Any) -> bool:
  return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class Span:
  start: int
  end: Optional[int] = None


@dataclass(frozen=True)
class TrailEntry:
  id: str
  spans: tuple[Span, ...]
  restored: bool = False
  ended: bool = False
  forked_from: Optional[dict[str, Any]] = None

  def item_at(self, step: int) -> int:
    """the record item held at a trail step, through any gaps between spans."""
    if not _ordinal(step):
      raise ValueError('step must be a non-negative integer')
    for span in self.spans:
      if span.end is None or step < span.end - span.start:
        return span.start + step
      step -= span.end - span.start
    raise IndexError('step exceeds trail spans')

  def step_at(self, item: int) -> Optional[int]:
    """the trail step holding an item, or None outside this entry's spans."""
    if not _ordinal(item):
      raise ValueError('item must be a non-negative integer')
    offset = 0
    for span in self.spans:
      if item >= span.start and (span.end is None or item < span.end):
        return offset + item - span.start
      if span.end is None:
        break
      offset += span.end - span.start
    return None

  def close(self, extent: int) -> 'TrailEntry':
    """close the open span at the first item the trail does not hold."""
    if not _ordinal(extent):
      raise ValueError('extent must be a non-negative integer')
    if self.spans[-1].end is not None:
      raise ValueError('trail has no open span')
    closed_length = 0
    for span in self.spans[:-1]:
      if span.end is None:
        raise ValueError('only the last span may be open')
      closed_length += span.end - span.start
    if extent < closed_length:
      raise ValueError('extent precedes the open span')
    last = Span(self.spans[-1].start, self.spans[-1].start + extent - closed_length)
    return replace(self, spans=(*self.spans[:-1], last))

  def to_wire(self) -> dict[str, Any]:
    return {
      'id': self.id,
      'spans': [[span.start, span.end] for span in self.spans],
      'restored': self.restored,
      'ended': self.ended,
      'forked_from': self.forked_from,
    }


def _entry(value: Any) -> TrailEntry:
  if not isinstance(value, dict) or set(value) != {
    'id',
    'spans',
    'restored',
    'ended',
    'forked_from',
  }:
    raise ValueError('trail entry has an invalid shape')
  if not is_lulid(value['id']):
    raise ValueError('trail id must be a lulid')
  if not isinstance(value['restored'], bool) or not isinstance(value['ended'], bool):
    raise ValueError('restored and ended must be booleans')
  raw_spans = value['spans']
  if not isinstance(raw_spans, list) or len(raw_spans) == 0:
    raise ValueError('spans must be a non-empty list')
  spans = []
  for span in raw_spans:
    if (
      not isinstance(span, list)
      or len(span) != 2
      or not _ordinal(span[0])
      or (span[1] is not None and (not _ordinal(span[1]) or span[1] < span[0]))
    ):
      raise ValueError('span must be a half-open range of non-negative integers')
    spans.append(Span(*span))
  edge = value['forked_from']
  if edge is not None:
    required = {'trail_id', 'step_id'}
    if not value['restored']:
      required.add('kind')
    if (
      not isinstance(edge, dict)
      or not required.issubset(edge)
      or not set(edge).issubset({'trail_id', 'step_id', 'kind'})
      or not isinstance(edge['trail_id'], str)
      or len(edge['trail_id']) == 0
      or not _ordinal(edge['step_id'])
      or ('kind' in edge and edge['kind'] not in ('resume', 'trail', 'branch'))
    ):
      raise ValueError('forked_from has an invalid edge shape')
  return TrailEntry(value['id'], tuple(spans), value['restored'], value['ended'], edge)


def _decode(value: Any) -> tuple[dict[str, list[TrailEntry]], Optional[list[str]]]:
  if (
    not isinstance(value, dict)
    or not {'format', 'records'}.issubset(value)
    or not set(value).issubset({'format', 'records', 'restoring'})
    or type(value['format']) is not int
    or value['format'] != 1
    or not isinstance(value['records'], dict)
  ):
    raise ValueError('expected a format-1 recording map')
  restoring = value.get('restoring')
  if 'restoring' in value and (
    not isinstance(restoring, list)
    or not all(isinstance(path, str) and len(path) > 0 for path in restoring)
  ):
    raise ValueError('restoring must be a list of file names')
  records = {}
  for record_id, record in value['records'].items():
    if not isinstance(record_id, str) or len(record_id) == 0:
      raise ValueError('record id must be a non-empty string')
    if (
      not isinstance(record, dict)
      or set(record) != {'trails'}
      or not isinstance(record['trails'], list)
      or len(record['trails']) == 0
    ):
      raise ValueError(f'record {record_id!r} must have a non-empty trails list')
    entries = [_entry(entry) for entry in record['trails']]
    if len({entry.id for entry in entries}) != len(entries):
      raise ValueError(f'record {record_id!r} has duplicate trail entries')
    previous_end = 0
    for entry_index, entry in enumerate(entries):
      for span_index, span in enumerate(entry.spans):
        if previous_end is None or span.start < previous_end:
          raise ValueError(f'record {record_id!r} has unordered or overlapping spans')
        if span.end is None and (
          entry_index != len(entries) - 1 or span_index != len(entry.spans) - 1
        ):
          raise ValueError('only the last span of the last trail may be open')
        previous_end = span.end
    records[record_id] = entries
  return records, restoring


@dataclass
class RecordingMap:
  """one writer under the session lock owns this map and its replacements."""

  path: Path
  records: dict[str, list[TrailEntry]] = field(default_factory=dict)

  @classmethod
  def load(cls, session_directory: Path) -> 'RecordingMap':
    path = session_directory / FILENAME
    try:
      raw = path.read_bytes()
    except FileNotFoundError:
      return cls(path)
    try:
      records, restoring = _decode(json.loads(raw))
      if restoring is not None:
        raise ValueError('restoring records are not supported by the recorder')
      if any(entry.restored for entries in records.values() for entry in entries):
        raise ValueError('restored entries are not supported by the recorder')
    except ValueError as exception:
      raise RecordingMapError(f'invalid recording map {path}: {exception}') from exception
    return cls(path, records)

  def set_entries(self, record_id: str, entries: list[TrailEntry]) -> None:
    """persist an entry replacement before exposing it to the next pass."""
    records = dict(self.records)
    if len(entries) == 0:
      records.pop(record_id, None)
    else:
      records[record_id] = entries
    candidate = RecordingMap(self.path, records)
    candidate.save()
    self.records = records

  def save(self) -> None:
    value = {
      'format': 1,
      'records': {
        record_id: {'trails': [entry.to_wire() for entry in entries]}
        for record_id, entries in self.records.items()
      },
    }
    try:
      _decode(value)
    except ValueError as exception:
      raise RecordingMapError(f'invalid recording map {self.path}: {exception}') from exception
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
      descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=f'.{self.path.name}.')
      temporary = Path(name)
      cleanup.callback(temporary.unlink, missing_ok=True)
      with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
      temporary.replace(self.path)
