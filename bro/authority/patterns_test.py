from itertools import product
from random import Random

from .oracle_test_helper import descriptors, matches, pattern_matches, powerset
from .patterns import Descriptor, Pattern, PatternSet, Predicate
from .values import Finite, IntegerInterval, Integers, LatticeInterval, Strings, Subsets


def test_all_two_option_rectangles_against_brute_force():
  base = frozenset({'a', 'b'})
  states = [
    (Finite(base, values), required) for values in powerset(base) for required in (False, True)
  ]
  patterns = [
    Pattern(
      'T',
      {'A': left[0], 'B': right[0]},
      frozenset(name for name, state in [('A', left), ('B', right)] if state[1]),
    )
    for left, right in product(states, repeat=2)
  ]
  cases = descriptors('T', {'A': ['a', 'b'], 'B': ['a', 'b']})
  for left, right in product(patterns, repeat=2):
    intersection = left.meet(right)
    difference = PatternSet(left.difference(right))
    expected_inclusion = True
    for descriptor in cases:
      first, second = matches(left, descriptor), matches(right, descriptor)
      assert left.matches(descriptor) == first
      assert (intersection is not None and intersection.matches(descriptor)) == (first and second)
      assert difference.matches(descriptor) == (first and not second)
      expected_inclusion &= not first or second
    assert PatternSet((left,)).within(PatternSet((right,))) == expected_inclusion


def test_union_inclusion_and_meet_across_families_and_types():
  random = Random(941)
  base = frozenset({'a', 'b'})
  families = {
    'A': [Strings(values, cofinite) for values in powerset(base) for cofinite in (False, True)],
    'B': [
      Subsets(base, (LatticeInterval(lower, upper),))
      for lower, upper in product(powerset(base), repeat=2)
      if lower <= upper
    ],
    'N': [
      Integers((IntegerInterval(lower, upper),))
      for lower, upper in [(None, 0), (0, None), (0, 0), (None, None)]
    ],
  }
  cases = descriptors(
    'T',
    {'A': ['a', 'b', 'fresh', 'another'], 'B': list(powerset(base)), 'N': [-100, -1, 0, 1, 100]},
  )
  cases += [Descriptor('U', case.values) for case in cases]

  def generate():
    return PatternSet(
      tuple(
        Pattern(
          random.choice(['T', 'T', 'U']),
          {
            name: random.choice(values)
            for name, values in families.items()
            if random.choice([False, True])
          },
          frozenset(name for name in families if random.choice([False, True])),
        )
        for _ in range(random.randrange(5))
      )
    )

  for _ in range(160):
    left, right = generate(), generate()
    expected = all(
      not pattern_matches(left, descriptor) or pattern_matches(right, descriptor)
      for descriptor in cases
    )
    assert left.within(right) == expected
    for descriptor in cases:
      assert left.meet(right).matches(descriptor) == (
        pattern_matches(left, descriptor) and pattern_matches(right, descriptor)
      )


def test_inclusion_requires_a_cover_by_the_whole_pattern_union():
  left = PatternSet((Pattern('T', {'A': Strings(frozenset({'a1', 'a2'}))}),))
  right = PatternSet(
    tuple(Pattern('T', {'A': Strings(frozenset({value}))}) for value in ['a1', 'a2'])
  )
  assert left.within(right)
  assert not left.within(PatternSet((right.patterns[0],)))
  assert not PatternSet.all('T').within(right)


def test_prepare_ignores_conditions_on_unset_options_only():
  pattern = Pattern('T', {'A': Strings(frozenset({'a1'}))}, frozenset({'A', 'F'}))
  assert pattern.matches(Descriptor('T'), prepare=True)
  assert not pattern.matches(Descriptor('T'))
  assert not pattern.matches(Descriptor('T', {'A': 'a2'}), prepare=True)
  assert pattern.matches(Descriptor('T', {'A': 'a1'}), prepare=True)


def test_unset_and_empty_subset_are_distinct():
  base = frozenset({'b'})
  unset = Pattern('T', {'B': Subsets(base)})
  empty = Pattern('T', {'B': Subsets(base).singleton(frozenset())}, frozenset({'B'}))
  assert unset.matches(Descriptor('T'))
  assert not unset.matches(Descriptor('T', {'B': frozenset()}))
  assert empty.matches(Descriptor('T', {'B': frozenset()}))
  assert not empty.matches(Descriptor('T'))
  intersection = unset.meet(empty)
  assert intersection is not None and intersection.is_empty


def test_inclusion_uses_the_declared_domain_not_the_larger_family_universe():
  domain = Integers((IntegerInterval(1, 100),))
  bounded = PatternSet((Pattern('T', {'N': domain}),))
  assert not PatternSet.all('T').within(bounded)
  assert PatternSet.all('T').within(bounded, {'T': {'N': domain}})


def test_predicate_patterns_against_brute_force():
  base = frozenset({'a', 'b'})
  first = Predicate('first', lambda descriptor, prepare: descriptor.values.get('A') == 'a')
  second = Predicate('second', lambda descriptor, prepare: descriptor.values.get('B') == 'b')
  options = [
    (Finite(base, values), required) for values in powerset(base) for required in (False, True)
  ]
  conditions = [
    (frozenset(held), frozenset(unheld))
    for held in powerset([first, second])
    for unheld in powerset([first, second])
  ]
  patterns = [
    Pattern(
      'T', {'A': values[0]}, frozenset({'A'}) if values[1] else frozenset(), frozenset(), *condition
    )
    for values, condition in product(options, conditions)
  ]
  cases = descriptors('T', {'A': ['a', 'b'], 'B': ['a', 'b']})
  for left, right in product(patterns, repeat=2):
    intersection = left.meet(right)
    difference = PatternSet(left.difference(right))
    expected_inclusion = True
    for descriptor in cases:
      for prepare in (False, True):
        first_match = matches(left, descriptor, prepare=prepare)
        second_match = matches(right, descriptor, prepare=prepare)
        assert left.matches(descriptor, prepare=prepare) == first_match
        assert intersection is not None
        assert intersection.matches(descriptor, prepare=prepare) == (first_match and second_match)
      first_match, second_match = matches(left, descriptor), matches(right, descriptor)
      assert difference.matches(descriptor) == (first_match and not second_match)
      expected_inclusion &= not first_match or second_match
    if PatternSet((left,)).within(PatternSet((right,))):
      assert expected_inclusion
