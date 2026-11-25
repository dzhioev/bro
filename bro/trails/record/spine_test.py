import json

import pytest

from bro.base.lulid import is_lulid, lulid
from bro.trails.local import LocalStore
from bro.trails.model import StartRequest
from bro.trails.record.recording_map import RecordingMap, Span, TrailEntry
from bro.trails.record.spine import MappedStart
from bro.trails.store import TrailCollision, TransientUnavailable


def _request(**fields):
  return StartRequest(
    harness='bro',
    version='test',
    interactive=False,
    surface='ask',
    bro='dev',
    native={'llm': {'type': 'echo', 'model': 'echo'}},
    body={'records': [{'kind': 'system_prompt', 'body': 'prompt'}]},
    **fields,
  )


def test_map_is_persisted_before_start_and_a_lost_reply_retries_the_same_request(
  tmp_path, monkeypatch
):
  recording_map = RecordingMap.load(tmp_path / 'session')
  request = _request()
  pending = MappedStart.prepare(recording_map, 'run', (Span(0),), request)
  entry = RecordingMap.load(recording_map.path.parent).records['run'][0]
  assert is_lulid(entry.id)
  assert entry.id == pending.request.id
  assert request.id is None
  store = LocalStore(tmp_path / 'store')
  original_start = store.start_trail
  requests = []

  def lose_first_reply(named):
    persisted = RecordingMap.load(recording_map.path.parent).records['run'][0]
    assert persisted.id == named.id
    requests.append(named.to_wire())
    response = original_start(named)
    if len(requests) == 1:
      raise TransientUnavailable('reply lost')
    return response

  monkeypatch.setattr(store, 'start_trail', lose_first_reply)
  with store:
    with pytest.raises(TransientUnavailable, match='reply lost'):
      pending.start(store)
    request.body['records'].append({'kind': 'user_input', 'body': 'later', 'turn_index': 0})
    request.native['llm']['model'] = 'changed'
    recording = pending.start(store)
    assert requests[0] == requests[1]
    assert recording.trail_id == entry.id
    assert recording.extent == 1
    recording.append([{'kind': 'user_input', 'body': 'continued', 'turn_index': 0}])
    assert pending.start(store).extent == 2
    assert [trail['id'] for trail in store.iter_trails()] == [entry.id]
    assert len(store.collect_steps(entry.id, extent=2)) == 2


def test_failed_map_write_sends_no_start_and_keeps_the_previous_map(tmp_path, monkeypatch):
  recording_map = RecordingMap.load(tmp_path)

  def fail_save(self):
    raise OSError('map not writable')

  monkeypatch.setattr(RecordingMap, 'save', fail_save)
  with pytest.raises(OSError, match='map not writable'):
    MappedStart.prepare(recording_map, 'run', (Span(0),), _request())
  assert recording_map.records == {}
  assert not recording_map.path.exists()


def test_mapped_start_keeps_explicit_wire_nulls_in_its_digest(tmp_path):
  request = StartRequest.from_wire({**_request().to_wire(), 'subject': None})
  pending = MappedStart.prepare(RecordingMap.load(tmp_path / 'session'), 'run', (Span(0),), request)
  with LocalStore(tmp_path / 'store') as store:
    recording = pending.start(store)
    assert 'subject' in pending.request.to_wire()
    assert store.get_trail(recording.trail_id)['start_sha256'] == pending.request.start_sha256
    assert pending.start(store).trail_id == recording.trail_id


def test_mapped_start_persists_the_edge_and_propagates_a_collision(tmp_path, monkeypatch):
  with LocalStore(tmp_path / 'store') as store:
    parent = store.start_trail(_request())
    edge = {'trail_id': parent['id'], 'step_id': 0, 'kind': 'resume'}
    monkeypatch.setattr('bro.trails.record.spine.lulid', lambda: parent['id'])
    recording_map = RecordingMap.load(tmp_path / 'session')
    pending = MappedStart.prepare(recording_map, 'run', (Span(1),), _request(forked_from=edge))
    assert recording_map.records['run'][0].forked_from == edge
    with pytest.raises(TrailCollision):
      pending.start(store)


@pytest.mark.parametrize(
  'fields', [{'id': lulid()}, {'lineage': {'segment': 'record', 'lines': []}}]
)
def test_mapped_start_rejects_preselected_ids_and_lineage(tmp_path, fields):
  with pytest.raises(ValueError, match='unnamed request without lineage'):
    MappedStart.prepare(RecordingMap.load(tmp_path), 'run', (Span(0),), _request(**fields))


def test_mapped_start_appends_to_a_known_record_after_its_span_is_closed(tmp_path):
  recording_map = RecordingMap.load(tmp_path)
  prior = TrailEntry(lulid(), (Span(0, 3),), ended=True)
  recording_map.set_entries('record', [prior])
  MappedStart.prepare(recording_map, 'record', (Span(3),), _request())
  persisted = json.loads(recording_map.path.read_text())['records']['record']['trails']
  assert len(persisted) == 2
  assert persisted[0] == prior.to_wire()
  assert persisted[1]['spans'] == [[3, None]]
