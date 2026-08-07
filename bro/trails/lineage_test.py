import pytest

from bro.trails.lineage import walk_header_chain


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
