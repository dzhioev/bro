import copy
import json
from pathlib import Path

import pytest

from bro.base.lulid import lulid
from bro.trails.record.recording_map import RecordingMap, RecordingMapError, Span, TrailEntry


def _wire():
  return {
    'format': 1,
    'records': {
      'conversation': {
        'trails': [
          {'id': lulid(), 'spans': [[0, 2]], 'restored': False, 'ended': True, 'forked_from': None},
          {
            'id': lulid(),
            'spans': [[2, 4], [7, None]],
            'restored': False,
            'ended': False,
            'forked_from': {'trail_id': lulid(), 'step_id': 1, 'kind': 'resume'},
          },
        ]
      }
    },
  }


def _load(tmp_path, value):
  (tmp_path / 'recording-map.json').write_text(json.dumps(value))
  return RecordingMap.load(tmp_path)


def test_missing_file_is_an_empty_map_without_creating_a_file(tmp_path):
  recording_map = RecordingMap.load(tmp_path)
  assert recording_map.records == {}
  assert list(tmp_path.iterdir()) == []


def test_persisted_map_round_trip_and_item_ordinals(tmp_path):
  wire = _wire()
  recording_map = _load(tmp_path, wire)
  first, last = recording_map.records['conversation']
  assert first.ended
  assert first.step_at(1) == 1
  assert first.step_at(2) is None
  assert [last.item_at(step) for step in range(5)] == [2, 3, 7, 8, 9]
  assert [last.step_at(item) for item in [0, 2, 3, 4, 6, 7, 8]] == [None, 0, 1, None, None, 2, 3]
  recording_map.save()
  assert json.loads((tmp_path / 'recording-map.json').read_text()) == wire
  assert RecordingMap.load(tmp_path).records == recording_map.records


@pytest.mark.parametrize(
  'value',
  [
    None,
    [],
    {},
    {'format': 1},
    {'format': True, 'records': {}},
    {'format': 2, 'records': {}},
    {'format': 1, 'records': []},
    {'format': 1, 'records': {}, 'progress': 0},
  ],
)
def test_refuses_wrong_top_level_shape(tmp_path, value):
  with pytest.raises(RecordingMapError, match='recording-map.json'):
    _load(tmp_path, value)


@pytest.mark.parametrize(
  'record', [None, [], {}, {'trails': None}, {'trails': []}, {'trails': [], 'extent': 1}]
)
def test_refuses_wrong_record_shape(tmp_path, record):
  with pytest.raises(RecordingMapError):
    _load(tmp_path, {'format': 1, 'records': {'conversation': record}})


def test_refuses_an_empty_record_id(tmp_path):
  wire = _wire()
  wire['records'][''] = wire['records'].pop('conversation')
  with pytest.raises(RecordingMapError, match='record id'):
    _load(tmp_path, wire)


@pytest.mark.parametrize('field', ['id', 'spans', 'restored', 'ended', 'forked_from'])
def test_refuses_a_missing_entry_field(tmp_path, field):
  wire = _wire()
  del wire['records']['conversation']['trails'][0][field]
  with pytest.raises(RecordingMapError, match='entry'):
    _load(tmp_path, wire)


@pytest.mark.parametrize(
  ('field', 'value'),
  [
    ('id', 'not-a-trail-id'),
    ('id', None),
    ('restored', 0),
    ('ended', 1),
    ('spans', []),
    ('spans', None),
    ('spans', [[True, 2]]),
    ('spans', [[0, False]]),
    ('spans', [[-1, 2]]),
    ('spans', [[1, 0]]),
    ('spans', [[0, 1.5]]),
    ('spans', [[0]]),
    ('spans', [[0, 1, 2]]),
    ('spans', ['bad']),
    ('extent', 1),
  ],
)
def test_refuses_malformed_entries(tmp_path, field, value):
  wire = _wire()
  wire['records']['conversation']['trails'][0][field] = value
  with pytest.raises(RecordingMapError):
    _load(tmp_path, wire)


@pytest.mark.parametrize(
  'edge',
  [
    [],
    {},
    {'trail_id': 'parent', 'step_id': 0},
    {'trail_id': '', 'step_id': 0, 'kind': 'resume'},
    {'trail_id': 'parent', 'step_id': True, 'kind': 'resume'},
    {'trail_id': 'parent', 'step_id': None, 'kind': 'resume'},
    {'trail_id': 'parent', 'step_id': -1, 'kind': 'resume'},
    {'trail_id': 'parent', 'step_id': 0, 'kind': 'attach'},
    {'trail_id': 'parent', 'step_id': 0, 'kind': 'resume', 'other': 1},
  ],
)
def test_refuses_invalid_edges(tmp_path, edge):
  wire = _wire()
  wire['records']['conversation']['trails'][1]['forked_from'] = edge
  with pytest.raises(RecordingMapError, match='edge'):
    _load(tmp_path, wire)


@pytest.mark.parametrize(
  ('first', 'last'),
  [
    ([[0, None]], [[2, None]]),
    ([[0, 2]], [[2, None], [4, 5]]),
    ([[0, 3]], [[2, None]]),
    ([[0, 2]], [[2, 5], [4, None]]),
    ([[0, 2]], [[5, 7], [2, 4]]),
  ],
)
def test_refuses_overlap_unordered_spans_and_nonfinal_open_spans(tmp_path, first, last):
  wire = _wire()
  entries = wire['records']['conversation']['trails']
  entries[0]['spans'], entries[1]['spans'] = first, last
  with pytest.raises(RecordingMapError, match='spans|span'):
    _load(tmp_path, wire)


@pytest.mark.parametrize('restoring', [[], ['bro/runs/new.jsonl'], None, 'file', [1]])
def test_recorder_refuses_restoring_state(tmp_path, restoring):
  wire = _wire()
  wire['restoring'] = restoring
  with pytest.raises(RecordingMapError, match='restoring'):
    _load(tmp_path, wire)


def test_recorder_refuses_restored_entries(tmp_path):
  wire = _wire()
  entry = wire['records']['conversation']['trails'][0]
  entry['restored'] = True
  with pytest.raises(RecordingMapError, match='restored'):
    _load(tmp_path, wire)


@pytest.mark.parametrize('raw', [b'{', b'\xff'])
def test_refuses_invalid_json(tmp_path, raw):
  (tmp_path / 'recording-map.json').write_bytes(raw)
  with pytest.raises(RecordingMapError, match='recording-map.json'):
    RecordingMap.load(tmp_path)


def test_replacement_is_atomic(tmp_path, monkeypatch):
  wire = _wire()
  recording_map = _load(tmp_path, wire)
  original_replace = Path.replace
  replacements = []

  def observe_replace(source, target):
    assert json.loads(target.read_text()) == wire
    replacements.append(json.loads(source.read_text()))
    return original_replace(source, target)

  monkeypatch.setattr(Path, 'replace', observe_replace)
  entries = recording_map.records['conversation']
  recording_map.set_entries('conversation', [entries[0], entries[1].close(5)])
  assert len(replacements) == 1
  assert replacements[0]['records']['conversation']['trails'][1]['spans'] == [[2, 4], [7, 10]]
  assert json.loads(recording_map.path.read_text()) == replacements[0]
  assert list(tmp_path.iterdir()) == [recording_map.path]


def test_failed_replacement_keeps_disk_and_memory_and_cleans_temporary_file(tmp_path, monkeypatch):
  wire = _wire()
  recording_map = _load(tmp_path, wire)
  before = copy.deepcopy(recording_map.records)

  def fail_replace(source, target):
    raise OSError('replacement failed')

  monkeypatch.setattr(Path, 'replace', fail_replace)
  with pytest.raises(OSError, match='replacement failed'):
    recording_map.set_entries('conversation', [])
  assert recording_map.records == before
  assert json.loads(recording_map.path.read_text()) == wire
  assert list(tmp_path.iterdir()) == [recording_map.path]


def test_invalid_replacement_does_not_touch_the_map(tmp_path):
  wire = _wire()
  recording_map = _load(tmp_path, wire)
  with pytest.raises(RecordingMapError):
    recording_map.set_entries('conversation', [TrailEntry(lulid(), (Span(-1),))])
  assert json.loads(recording_map.path.read_text()) == wire


def test_closing_a_span_keeps_ordinal_lookup_and_rejects_steps_past_its_extent():
  entry = TrailEntry(lulid(), (Span(0, 2), Span(5)))
  closed = entry.close(4)
  assert [closed.item_at(step) for step in range(4)] == [0, 1, 5, 6]
  assert closed.step_at(7) is None
  with pytest.raises(IndexError):
    closed.item_at(4)
  with pytest.raises(ValueError, match='precedes'):
    entry.close(1)
  with pytest.raises(ValueError, match='no open span'):
    closed.close(4)
  with pytest.raises(ValueError):
    entry.item_at(-1)
  with pytest.raises(ValueError):
    entry.step_at(True)
  with pytest.raises(ValueError):
    entry.close(False)


def test_refuses_two_entries_assigning_the_same_trails_steps_to_different_items(tmp_path):
  wire = _wire()
  entries = wire['records']['conversation']['trails']
  entries[1]['id'] = entries[0]['id']
  with pytest.raises(RecordingMapError, match='duplicate trail'):
    _load(tmp_path, wire)
