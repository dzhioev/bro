from itertools import product
from random import Random

import pytest

from .authority import Atom, Authority, Cap, Entry, LazyEntry, Pair, Permission, partition
from .oracle_test_helper import descriptors, evaluate, pattern_matches, powerset
from .patterns import Descriptor, Pattern, PatternSet
from .values import Strings


def test_expression_evaluation_matches_boolean_oracle():
  random = Random(873)
  types = ('T', 'U')
  permissions = [Permission(type) for type in types] + [
    Permission(type, 'A', Strings(values, cofinite))
    for type in types
    for values in powerset(['a', 'b'])
    for cofinite in (False, True)
  ]
  cases = [
    case for type in types for case in descriptors(type, {'A': ['a', 'b', 'fresh', 'another']})
  ]
  atoms = [Atom(type) for type in types] + [
    Atom(type, 'A', value) for type in types for value in ['a', 'b', 'fresh', 'another']
  ]

  def patterns(type=None):
    return PatternSet(
      tuple(
        Pattern(
          type or random.choice(types),
          {'A': Strings(random.choice(powerset(['a', 'b'])))},
          frozenset({'A'}) if random.choice([False, True]) else frozenset(),
        )
        for _ in range(random.randrange(3))
      )
    )

  def authority(cap=False):
    entries = tuple(
      Entry(permission, Pair(patterns(permission.type), patterns()))
      for permission in random.sample(permissions, random.randrange(5))
    )
    return Cap(entries) if cap else Authority(entries)

  for _ in range(80):
    left, right, cap = authority(), authority(), authority(True)
    expression = left.join(right).meet(cap).join(Authority.top().meet(authority(True)))
    for atom, probe in product(atoms, cases):
      pair = expression.evaluate(atom, probe, types)
      assert pair.use.matches(probe) == evaluate(expression, atom, probe, probe, give=False)
      assert pair.give.matches(probe) == evaluate(expression, atom, probe, probe, give=True)


def test_partition_is_disjoint_and_preserves_all_permission_memberships():
  permissions = [
    Permission('T', 'A', Strings(values, cofinite))
    for values in powerset(['a', 'b'])
    for cofinite in (False, True)
  ]
  permissions += [Permission('T'), Permission('U')]
  regions = partition(permissions)
  for left, right in product(regions, repeat=2):
    if left is not right:
      assert left.intersection(right) is None
  for atom in [
    Atom('T'),
    Atom('U'),
    *(Atom('T', 'A', value) for value in ['a', 'b', 'fresh', 'another']),
  ]:
    containing = [region for region in regions if region.contains(atom)]
    assert len(containing) == 1
    for permission in permissions:
      assert (containing[0].intersection(permission) is not None) == permission.contains(atom)


def test_lazy_entries_resolve_only_for_a_matching_atom_and_checked_start():
  calls = []

  def resolve(descriptor):
    calls.append(descriptor)
    return Pair(
      PatternSet(
        (Pattern('T', {'A': Strings(frozenset({descriptor.values['A']}))}, frozenset({'A'})),)
      )
    )

  lazy = Authority((LazyEntry(Permission('T', 'A', Strings(frozenset({'a'}))), resolve),))
  descriptor = Descriptor('T', {'A': 'a'})
  assert lazy.evaluate(Atom('T'), descriptor, ('T',)).is_empty
  assert lazy.evaluate(Atom('T', 'A', 'b'), descriptor, ('T',)).is_empty
  assert not calls
  assert lazy.evaluate(Atom('T', 'A', 'a'), descriptor, ('T',)).use.matches(descriptor)
  assert calls == [descriptor]


def test_caps_default_to_top_and_overlap_meets_instead_of_joins():
  first = PatternSet((Pattern('T', {'A': Strings(frozenset({'a'}))}),))
  second = PatternSet((Pattern('T', {}, frozenset({'A'})),))
  cap = Cap(
    (Entry(Permission('U'), Pair(first, first)), Entry(Permission('U'), Pair(second, second)))
  )
  for descriptor in descriptors('T', {'A': ['a', 'b']}):
    assert cap.evaluate(Atom('U'), descriptor, ('T', 'U')).give.matches(descriptor) == (
      pattern_matches(first, descriptor) and pattern_matches(second, descriptor)
    )
  assert cap.evaluate(Atom('T'), Descriptor('T'), ('T', 'U')).use.matches(Descriptor('T'))


def test_crossing_a_region_boundary_is_an_error():
  authority = Authority((Entry(Permission('T', 'A', Strings(frozenset({'a'}))), Pair()),))
  with pytest.raises(ValueError, match='boundary'):
    authority.evaluate_region(Permission('T', 'A', Strings(cofinite=True)), Descriptor('T'), ('T',))
