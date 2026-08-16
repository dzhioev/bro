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
      surface='cw',
      body={'records': lines},
      native={
        'llm': {'type': 'claude'},
        'segment': segment,
        'cw_command': 'cw ss ws',
        'harness_version': 'test',
      },
      forked_from=forked_from,
    )
  )['id']


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

  def test_a_malformed_index_match_fails_fast(self, store):
    class Index:
      def find_segment_steps(self, segments: set[str], uuids: set[str]) -> list[dict]:
        del segments, uuids
        return [{'trail_id': 'T1', 'step_id': None, 'uuid': 'u1', 'header': {}}]

      def get_trail(self, trail_id: str) -> dict:
        raise AssertionError('unreachable')

      def get_step_uuids(self, trail_id: str, *, through: Optional[int] = None) -> list[dict]:
        raise AssertionError('unreachable')

      def step_payload_hashes(self, trail_id: str, step_ids: list[int]) -> dict[int, str]:
        raise AssertionError('unreachable')

    with pytest.raises(ValueError, match='malformed lineage index match'):
      resolve(_evidence('seg-1', [_user('hello', 'u1')]), Index())
