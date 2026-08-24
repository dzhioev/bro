import pytest

from bro.trails.lineage import LineageHead, walk_header_chain


def _fold(head: LineageHead, count: int, *, start: int = 0, uuids: bool = True) -> LineageHead:
  for step_id in range(start, start + count):
    head.fold(
      step_id=step_id,
      uuid=f'u{step_id}' if uuids else None,
      payload_sha256=f'digest-{step_id}',
    )
  return head


class TestLineageHead:
  def test_remembers_the_first_record_the_newest_rows_and_the_final_digest(self):
    head = _fold(LineageHead(), 25)

    assert head.chain_first_uuid == 'u0'
    assert head.last_row_digest == 'digest-24'
    assert [row.step_id for row in head.tail] == list(range(5, 25))
    assert head.tail[-1] == (24, 'u24', 'digest-24')

  def test_a_final_row_without_a_uuid_still_sets_the_digest(self):
    head = _fold(_fold(LineageHead(), 2), 1, start=2, uuids=False)

    assert head.last_row_digest == 'digest-2'
    assert [row.uuid for row in head.tail] == ['u0', 'u1']

  def test_an_inherited_head_keeps_only_the_first_record(self):
    head = _fold(LineageHead(), 3).inherited()

    assert head == LineageHead(chain_first_uuid='u0')

  def test_a_stored_head_round_trips_and_keeps_the_first_record_across_folds(self):
    stored = LineageHead.stored({'lineage_head': _fold(LineageHead(), 3).fields()})

    assert stored == _fold(LineageHead(), 3)
    assert _fold(stored, 1, start=3).chain_first_uuid == 'u0'

  def test_a_header_without_a_head_folds_from_empty(self):
    assert LineageHead.stored({}) == LineageHead()

  @pytest.mark.parametrize(
    'stored',
    [
      {'chain_first_uuid': 'u0', 'unknown': 1},
      {'tail': {}},
      {'tail': [['0', 'u0', 'digest']]},
      {'tail': [[0, 'u0']]},
      {'last_row_digest': 7},
    ],
  )
  def test_a_malformed_stored_head_fails_fast(self, stored):
    with pytest.raises(ValueError):
      LineageHead.stored({'lineage_head': stored})


def test_walks_headers_root_first_with_each_child_bound():
  headers = {
    'root': {'id': 'root'},
    'middle': {'id': 'middle', 'forked_from': {'trail_id': 'root', 'step_id': 4}},
    'child': {'id': 'child', 'forked_from': {'trail_id': 'middle', 'step_id': 2}},
  }

  chain = walk_header_chain(headers['child'], lambda trail_id: headers[trail_id])

  assert [(header['id'], bound) for header, bound in chain] == [
    ('root', {'trail_id': 'root', 'step_id': 4}),
    ('middle', {'trail_id': 'middle', 'step_id': 2}),
    ('child', None),
  ]


def test_rejects_a_cycle():
  headers = {
    'one': {'id': 'one', 'forked_from': {'trail_id': 'two', 'step_id': 1}},
    'two': {'id': 'two', 'forked_from': {'trail_id': 'one', 'step_id': 2}},
  }

  with pytest.raises(ValueError, match='cycles through one'):
    walk_header_chain(headers['one'], lambda trail_id: headers[trail_id])


def test_rejects_a_parent_lookup_returning_the_wrong_trail():
  child = {'id': 'child', 'forked_from': {'trail_id': 'parent', 'step_id': 1}}

  with pytest.raises(ValueError, match="lookup for 'parent' returned trail 'other'"):
    walk_header_chain(child, lambda trail_id: {'id': 'other'})


@pytest.mark.parametrize('pointer', [{'step_id': 1}, {'trail_id': 'parent', 'step_id': '1'}])
def test_rejects_a_malformed_pointer(pointer):
  header = {'id': 'child', 'forked_from': pointer}

  with pytest.raises(ValueError, match='malformed forked_from'):
    walk_header_chain(header, lambda trail_id: {'id': trail_id})
