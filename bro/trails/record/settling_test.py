import json
from dataclasses import replace

import pytest

from bro.base.lulid import lulid
from bro.trails.local import LocalStore
from bro.trails.model import StartRequest
from bro.trails.record.recording_map import RecordingMap, RecordingMapError, Span, TrailEntry
from bro.trails.record.settling import settle_record, trail_extent
from bro.trails.store import PermissionDenied, TrailNotFound, TransientUnavailable


def _request():
  return StartRequest(
    harness='bro',
    version='test',
    interactive=False,
    surface='ask',
    bro='dev',
    native={'llm': {'type': 'echo', 'model': 'echo'}},
    body={'records': []},
  )


def _items(count):
  return [
    {'kind': 'user_input', 'body': f'item {index}', 'turn_index': index} for index in range(count)
  ]


def _trail(store, records):
  started = store.start_trail(_request())
  if len(records) > 0:
    store.append_records(started['id'], 0, records)
  return started['id']


@pytest.fixture
def record_only_store(tmp_path, monkeypatch):
  store = LocalStore(tmp_path / 'store')

  def refuse_read(*args, **kwargs):
    raise PermissionDenied('read is not permitted')

  for method in ('get_trail', 'get_step', 'get_steps', 'get_messages', 'list_trails', 'get_tool'):
    monkeypatch.setattr(store, method, refuse_read)
  with store:
    yield store


def _map(tmp_path, entries):
  recording_map = RecordingMap.load(tmp_path / 'session')
  recording_map.set_entries('record', entries)
  return recording_map


def test_extent_probe_uses_append_for_empty_nonempty_and_missing_trails(record_only_store):
  store = record_only_store
  assert trail_extent(store, _trail(store, [])) == 0
  assert trail_extent(store, _trail(store, _items(4))) == 4
  with pytest.raises(TrailNotFound):
    trail_extent(store, lulid())


def test_unknown_record_needs_no_store_operation(tmp_path, monkeypatch, record_only_store):
  def unexpected_append(*args, **kwargs):
    raise AssertionError('unknown record must not reach the store')

  monkeypatch.setattr(record_only_store, 'append_records', unexpected_append)
  assert settle_record(RecordingMap.load(tmp_path), 'unknown', record_only_store, []) is None


def test_settles_last_first_removing_never_started_and_empty_trails(
  tmp_path, record_only_store, monkeypatch, capsys
):
  store = record_only_store
  records = _items(6)
  verified = _trail(store, records[:2])
  empty = _trail(store, [])
  never_started = lulid()
  entries = [
    TrailEntry(verified, (Span(0, 2),), ended=True),
    TrailEntry(empty, (Span(2, 4),)),
    TrailEntry(never_started, (Span(4),)),
  ]
  recording_map = _map(tmp_path, entries)
  calls = []
  append = store.append_records

  def observe_append(trail_id, offset, payloads, **kwargs):
    calls.append((trail_id, offset, payloads))
    return append(trail_id, offset, payloads, **kwargs)

  monkeypatch.setattr(store, 'append_records', observe_append)
  settled = settle_record(recording_map, 'record', store, records)
  assert settled is not None
  assert (settled.entry, settled.extent) == (entries[0], 2)
  assert calls == [
    (never_started, 0, []),
    (empty, 0, []),
    (verified, 0, []),
    (verified, 1, [records[1]]),
  ]
  assert RecordingMap.load(recording_map.path.parent).records == {'record': entries[:1]}
  assert empty in capsys.readouterr().err
  assert trail_extent(store, empty) == 0


@pytest.mark.parametrize('missing', [False, True])
def test_an_empty_or_missing_last_entry_leaves_an_unknown_record(
  tmp_path, record_only_store, missing
):
  trail_id = lulid() if missing else _trail(record_only_store, [])
  recording_map = _map(tmp_path, [TrailEntry(trail_id, (Span(0),))])
  assert settle_record(recording_map, 'record', record_only_store, []) is None
  assert RecordingMap.load(recording_map.path.parent).records == {}


def test_verification_uses_spans_not_local_item_ordinals_and_can_close_the_span(
  tmp_path, record_only_store
):
  store = record_only_store
  records = _items(12)
  payloads = [records[2], records[3], records[8], records[9]]
  trail_id = _trail(store, payloads)
  entry = TrailEntry(trail_id, (Span(2, 4), Span(8)))
  recording_map = _map(tmp_path, [entry])
  settled = settle_record(recording_map, 'record', store, records)
  assert settled is not None
  assert settled.extent == 4
  recording_map.set_entries('record', [settled.entry.close(settled.extent)])
  closed = RecordingMap.load(recording_map.path.parent).records['record'][0]
  assert closed.spans == (Span(2, 4), Span(8, 10))
  assert trail_extent(store, trail_id) == 4


@pytest.mark.parametrize('damage', ['different-last-row', 'past-closed-spans', 'past-local-record'])
def test_unverified_last_entry_gives_up_all_entries_without_changing_store(
  tmp_path, record_only_store, capsys, damage
):
  store = record_only_store
  records = _items(6)
  first = _trail(store, records[:2])
  last = _trail(store, records[2:5])
  entry = TrailEntry(last, (Span(2),))
  if damage == 'different-last-row':
    records[4] = {**records[4], 'body': 'edited'}
  elif damage == 'past-closed-spans':
    entry = replace(entry, spans=(Span(2, 4),))
  else:
    records = records[:4]
  recording_map = _map(tmp_path, [TrailEntry(first, (Span(0, 2),)), entry])
  assert settle_record(recording_map, 'record', store, records) is None
  assert RecordingMap.load(recording_map.path.parent).records == {}
  diagnostics = capsys.readouterr().err
  assert first in diagnostics and last in diagnostics
  assert trail_extent(store, first) == 2
  assert trail_extent(store, last) == 3


@pytest.mark.parametrize('operation', ['probe', 'verify'])
@pytest.mark.parametrize('error', [TransientUnavailable, PermissionDenied])
def test_store_errors_propagate_without_giving_up_an_entry(
  tmp_path, record_only_store, monkeypatch, operation, error
):
  store = record_only_store
  records = _items(2)
  entry = TrailEntry(_trail(store, records), (Span(0),))
  recording_map = _map(tmp_path, [entry])
  before = recording_map.path.read_bytes()
  append = store.append_records

  def fail(trail_id, offset, payloads, **kwargs):
    if (len(payloads) == 0) == (operation == 'probe'):
      raise error('store refused')
    return append(trail_id, offset, payloads, **kwargs)

  monkeypatch.setattr(store, 'append_records', fail)
  with pytest.raises(error, match='store refused'):
    settle_record(recording_map, 'record', store, records)
  assert recording_map.path.read_bytes() == before
  assert recording_map.records['record'] == [entry]


def test_verification_uses_the_native_line_payload_verbatim(tmp_path, record_only_store):
  store = record_only_store
  body = json.dumps(
    {'kind': 'system_prompt', 'body': 'prompt', 'step': 0, 'at': '2026-01-01T00:00:00Z'}
  )
  records = [{'run_log': 1, 'body': body}]
  entry = TrailEntry(_trail(store, records), (Span(0),))
  recording_map = _map(tmp_path, [entry])
  assert settle_record(recording_map, 'record', store, records) is not None


def test_restored_entries_are_refused_even_in_a_programmatically_built_map(
  tmp_path, record_only_store
):
  recording_map = RecordingMap(
    tmp_path / 'recording-map.json',
    {'record': [TrailEntry(lulid(), (Span(0, 1),), restored=True, ended=True)]},
  )
  with pytest.raises(RecordingMapError, match='restored'):
    settle_record(recording_map, 'record', record_only_store, _items(1))


@pytest.mark.parametrize('extent', [None, True, -1, '1'])
def test_invalid_probe_extent_is_not_accepted(tmp_path, record_only_store, monkeypatch, extent):
  monkeypatch.setattr(record_only_store, 'append_records', lambda *args: {'extent': extent})
  with pytest.raises(RuntimeError, match='invalid extent'):
    trail_extent(record_only_store, lulid())


def test_a_verification_response_must_explicitly_confirm_a_duplicate(
  tmp_path, record_only_store, monkeypatch
):
  records = _items(1)
  entry = TrailEntry(_trail(record_only_store, records), (Span(0),))
  recording_map = _map(tmp_path, [entry])
  append = record_only_store.append_records

  def unconfirmed(trail_id, offset, payloads, **kwargs):
    response = append(trail_id, offset, payloads, **kwargs)
    response.pop('duplicate', None)
    return response

  monkeypatch.setattr(record_only_store, 'append_records', unconfirmed)
  with pytest.raises(RuntimeError, match='no duplicate'):
    settle_record(recording_map, 'record', record_only_store, records)
  assert recording_map.records['record'] == [entry]
