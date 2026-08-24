import json
from pathlib import Path
from typing import Any, Optional

import pytest

from bro.trails.claude_lineage import resolve
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest, payload_sha256


def _record(**fields: Any) -> str:
  return json.dumps({'version': '2.1.216', 'timestamp': '2026-01-01T00:00:00.000Z', **fields})


def _user(text: str, uuid: str) -> str:
  return _record(type='user', uuid=uuid, message={'content': text})


def _meta() -> str:
  return _record(type='mode', mode='normal')


def _uuid_of(line: str) -> Optional[str]:
  record = json.loads(line)
  uuid = record.get('uuid')
  return uuid if isinstance(uuid, str) else None


def _evidence(segment: str, lines: list[str], *, related: tuple[str, ...] = ()) -> dict:
  return {
    'segment': segment,
    'lines': [[_uuid_of(line), payload_sha256(line)] for line in lines],
    'related_segments': list(related),
  }


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
  return LocalStore(tmp_path / 'trails')


def _blaze(
  store: LocalStore, segment: str, lines: list[str], *, forked_from: Optional[dict] = None
) -> str:
  return store.blaze(
    BlazeRequest(
      harness='claude',
      version='test',
      interactive=True,
      surface='ride',
      body={'records': lines},
      native={
        'llm': {'type': 'claude'},
        'segment': segment,
        'ride_command': 'ride along ws',
        'harness_version': 'test',
      },
      forked_from=forked_from,
    )
  )['id']


class _IndexSpy:
  """a store that records the lineage lookups the resolver made of it."""

  def __init__(self, store: LocalStore) -> None:
    self._store = store
    self.segment_lookups: list[set[str]] = []
    self.record_probes: list[str] = []

  def find_segment_trails(self, segments: set[str]) -> list[dict]:
    self.segment_lookups.append(set(segments))
    return self._store.find_segment_trails(segments)

  def holds_record(self, trail_ids: set[str], uuid: str) -> bool:
    self.record_probes.append(uuid)
    return self._store.holds_record(trail_ids, uuid)


def _recorded_lines(count: int) -> list[str]:
  return [_user(f'line {index}', f'u{index}') for index in range(count)]


class TestSameSegment:
  def test_resume_forks_from_the_recorded_final_row(self, store):
    recorded = [_user('hello', 'u1'), _user('again', 'u2')]
    trail_id = _blaze(store, 'seg-1', recorded)

    decision = resolve(_evidence('seg-1', [*recorded, _user('more', 'u3')]), store)

    assert decision.adopt is True
    assert decision.forked_from == {'trail_id': trail_id, 'step_id': 1}
    assert decision.chunks == [[2, 2]]

  def test_a_file_that_has_not_grown_is_not_adopted(self, store):
    recorded = [_user('hello', 'u1')]
    _blaze(store, 'seg-1', recorded)

    decision = resolve(_evidence('seg-1', recorded), store)

    assert decision.adopt is False
    assert decision.reason == 'no line past the recorded extent yet'

  def test_a_rewritten_line_fails_the_anchors_and_roots(self, store):
    recorded = [_user('hello', 'u1'), _user('again', 'u2')]
    _blaze(store, 'seg-1', recorded)
    rewritten = [_user('tampered', 'u1'), recorded[1], _user('more', 'u3')]

    decision = resolve(_evidence('seg-1', rewritten), store)

    assert decision.forked_from is None
    assert decision.reason == 'no verified parent'

  def test_an_unrecorded_segment_roots(self, store):
    decision = resolve(_evidence('seg-1', [_user('hello', 'u1')]), store)

    assert decision.adopt is True
    assert decision.forked_from is None
    assert decision.chunks == [[0, 0]]

  def test_a_transcript_without_records_is_not_adopted(self, store):
    decision = resolve(_evidence('seg-1', [_meta()]), store)

    assert decision.adopt is False
    assert decision.reason == 'transcript carries no record yet'

  def test_a_fork_inherits_the_conversations_first_record(self, store):
    recorded = [_user('hello', 'u1'), _user('again', 'u2')]
    parent_id = _blaze(store, 'seg-1', recorded)

    forked_id = _blaze(store, 'seg-1', [], forked_from={'trail_id': parent_id, 'step_id': 1})

    head = store.get_trail(forked_id)['native']['lineage_head']
    assert head == {'chain_first_uuid': 'u1', 'tail': [], 'last_row_digest': None}


class TestCopiedHistory:
  def test_a_verified_copy_forks_and_keeps_only_the_new_lines(self, store):
    recorded = [_user('hello', 'u1'), _user('again', 'u2')]
    trail_id = _blaze(store, 'seg-1', recorded)
    copied = [_meta(), *recorded, _user('resumed', 'u3')]

    decision = resolve(_evidence('seg-2', copied, related=('seg-1',)), store)

    assert decision.forked_from == {'trail_id': trail_id, 'step_id': 1}
    assert decision.chunks == [[0, 1], [3, 3]]

  def test_a_copy_may_drop_trailing_ephemera(self, store):
    recorded = [_user('hello', 'u1'), _user('again', 'u2'), _user('dropped', 'u3')]
    trail_id = _blaze(store, 'seg-1', recorded)
    copied = [*recorded[:2], _user('resumed', 'u4')]

    decision = resolve(_evidence('seg-2', copied, related=('seg-1',)), store)

    assert decision.forked_from == {'trail_id': trail_id, 'step_id': 1}
    assert decision.chunks == [[2, 2]]

  def test_a_copy_missing_the_parents_first_record_roots(self, store):
    recorded = [_user('hello', 'u1'), _user('again', 'u2')]
    _blaze(store, 'seg-1', recorded)
    copied = [recorded[1], _user('resumed', 'u3')]

    decision = resolve(_evidence('seg-2', copied, related=('seg-1',)), store)

    assert decision.forked_from is None

  def test_a_copy_ending_inside_the_chain_is_not_adopted_yet(self, store):
    recorded = [_user('hello', 'u1'), _user('again', 'u2')]
    _blaze(store, 'seg-1', recorded)

    decision = resolve(_evidence('seg-2', recorded, related=('seg-1',)), store)

    assert decision.adopt is False
    assert decision.reason == 'history copy still being written'

  def test_the_copy_starts_at_the_earliest_line_an_ancestor_holds(self, store):
    root_lines = [_user('hello', 'u1')]
    root_id = _blaze(store, 'seg-1', root_lines)
    parent_lines = [_user('again', 'u2')]
    parent_id = _blaze(
      store, 'seg-1', parent_lines, forked_from={'trail_id': root_id, 'step_id': 0}
    )
    copied = [_meta(), *root_lines, *parent_lines, _user('resumed', 'u3')]

    decision = resolve(_evidence('seg-2', copied, related=('seg-1',)), store)

    assert decision.forked_from == {'trail_id': parent_id, 'step_id': 0}
    # the copy re-serializes the root's records too, so only the head ephemera
    # and the new tail are this trail's own
    assert decision.chunks == [[0, 1], [3, 3]]

  def test_a_copy_of_a_forked_chain_anchors_on_the_newest_head(self, store):
    root_lines = [_user('hello', 'u1')]
    root_id = _blaze(store, 'seg-1', root_lines)
    middle_lines = [_user('again', 'u2')]
    middle_id = _blaze(
      store, 'seg-1', middle_lines, forked_from={'trail_id': root_id, 'step_id': 0}
    )
    parent_lines = [_user('third', 'u3')]
    parent_id = _blaze(
      store, 'seg-1', parent_lines, forked_from={'trail_id': middle_id, 'step_id': 0}
    )
    spy = _IndexSpy(store)
    copied = [_meta(), *root_lines, *middle_lines, *parent_lines, _user('resumed', 'u4')]

    decision = resolve(_evidence('seg-2', copied, related=('seg-1',)), spy)

    assert decision.forked_from == {'trail_id': parent_id, 'step_id': 0}
    assert decision.chunks == [[0, 1], [4, 4]]
    assert spy.segment_lookups == [{'seg-1', 'seg-2'}]
    assert spy.record_probes == []

  def test_a_copy_short_of_the_remembered_rows_waits_on_one_uuid_probe(self, store):
    recorded = _recorded_lines(25)
    _blaze(store, 'seg-1', recorded)
    spy = _IndexSpy(store)

    # claude re-serializes the history in order, so a copy read mid-write holds
    # the conversation's opening and nothing the head remembers yet
    decision = resolve(_evidence('seg-2', recorded[:4], related=('seg-1',)), spy)

    assert decision.adopt is False
    assert decision.reason == 'history copy still being written'
    assert spy.record_probes == ['u3']

  def test_a_file_whose_newest_record_the_family_does_not_store_roots(self, store):
    recorded = _recorded_lines(25)
    _blaze(store, 'seg-1', recorded)
    copied = [*recorded[:4], _user('elsewhere', 'u-elsewhere')]

    decision = resolve(_evidence('seg-2', copied, related=('seg-1',)), store)

    assert decision.forked_from is None
    assert decision.reason == 'no verified parent'

  def test_a_two_chunk_trail_is_re_anchored_from_its_matched_rows(self, store):
    """a trail born from a copy holds a head chunk plus a tail, so its rows are
    not one contiguous run of the file it recorded."""
    root_lines = [_user('hello', 'u1')]
    root_id = _blaze(store, 'seg-1', root_lines)
    forked_file = [_meta(), *root_lines, _user('resumed', 'u2')]
    # the trail recording seg-2 holds the head ephemera and the tail, not the copy
    forked_id = _blaze(
      store,
      'seg-2',
      [forked_file[0], forked_file[2]],
      forked_from={'trail_id': root_id, 'step_id': 0},
    )

    decision = resolve(_evidence('seg-2', [*forked_file, _user('more', 'u3')]), store)

    assert decision.forked_from == {'trail_id': forked_id, 'step_id': 1}
    assert decision.chunks == [[3, 3]]


class TestHeaderReads:
  def test_one_segment_lookup_answers_a_resume_of_any_length(self, store):
    recorded = _recorded_lines(80)
    trail_id = _blaze(store, 'seg-1', recorded)
    spy = _IndexSpy(store)

    decision = resolve(_evidence('seg-1', [*recorded, _user('more', 'u-new')]), spy)

    assert decision.forked_from == {'trail_id': trail_id, 'step_id': 79}
    assert spy.segment_lookups == [{'seg-1'}]
    assert spy.record_probes == []

  def test_a_parent_far_behind_the_transcripts_newest_lines_is_still_found(self, store):
    recorded = [_user('hello', 'u-parent')]
    trail_id = _blaze(store, 'seg-1', recorded)

    # the transcript grew well past the recorded extent before this adoption
    decision = resolve(_evidence('seg-1', [*recorded, *_recorded_lines(80)]), store)

    assert decision.forked_from == {'trail_id': trail_id, 'step_id': 0}
    assert decision.chunks == [[1, 1]]

  def test_a_line_rewritten_inside_the_remembered_window_fails_the_digests(self, store):
    recorded = _recorded_lines(80)
    _blaze(store, 'seg-1', recorded)
    tampered = [*recorded[:79], _user('tampered', 'u79'), _user('more', 'u-new')]

    decision = resolve(_evidence('seg-1', tampered), store)

    assert decision.forked_from is None
    assert decision.reason == 'no verified parent'

  def test_a_line_rewritten_before_the_remembered_window_is_not_checked(self, store):
    """the head remembers a bounded window of rows, so a rewrite older than it
    verifies — the rigor the header-only resolution trades away."""
    recorded = _recorded_lines(80)
    trail_id = _blaze(store, 'seg-1', recorded)
    tampered = [_user('tampered', 'u0'), *recorded[1:], _user('more', 'u-new')]

    decision = resolve(_evidence('seg-1', tampered), store)

    assert decision.forked_from == {'trail_id': trail_id, 'step_id': 79}


class TestEvidence:
  @pytest.mark.parametrize(
    'evidence',
    [
      {'lines': []},
      {'segment': '', 'lines': []},
      {'segment': 'seg-1', 'lines': {}},
      {'segment': 'seg-1', 'lines': [], 'related_segments': [7]},
      {'segment': 'seg-1', 'lines': [['uuid-1']]},
      {'segment': 'seg-1', 'lines': [[1, 'digest']]},
    ],
  )
  def test_malformed_evidence_fails_fast(self, store, evidence):
    with pytest.raises(ValueError):
      resolve(evidence, store)

  def test_a_malformed_index_trail_fails_fast(self, store):
    class Index:
      def find_segment_trails(self, segments: set[str]) -> list[dict]:
        del segments
        return [{'id': 'T1'}]

      def holds_record(self, trail_ids: set[str], uuid: str) -> bool:
        raise AssertionError('unreachable')

    with pytest.raises(ValueError, match='malformed lineage index trail'):
      resolve(_evidence('seg-1', [_user('hello', 'u1')]), Index())
