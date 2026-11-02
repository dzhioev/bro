from collections.abc import Sequence
from itertools import combinations_with_replacement, product

import pytest

from .oracle_test_helper import member, powerset
from .values import (
  Finite,
  IntegerInterval,
  Integers,
  LatticeInterval,
  Scalars,
  StringGrammar,
  Strings,
  Subsets,
  Value,
  ValueSet,
)


def check_family(sets: Sequence[ValueSet], witnesses: Sequence[Value]) -> None:
  for left, right in product(sets, repeat=2):
    intersection = left.intersection(right)
    difference = left.difference(right)
    expected_intersection = {
      value for value in witnesses if member(left, value) and member(right, value)
    }
    expected_difference = {
      value for value in witnesses if member(left, value) and not member(right, value)
    }
    assert {value for value in witnesses if intersection.contains(value)} == expected_intersection
    assert {value for value in witnesses if difference.contains(value)} == expected_difference
    assert intersection.is_empty == (not expected_intersection)
    assert difference.is_empty == (not expected_difference)
  for values in sets:
    for value in witnesses:
      assert values.contains(value) == member(values, value)
      assert values.intersection(values.singleton(value)).is_empty == (not member(values, value))


def test_finite_and_cofinite_strings_against_oracle():
  sets = [
    Strings(members, cofinite)
    for members in powerset(['a', 'b', 'c'])
    for cofinite in (False, True)
  ]
  check_family(sets, ['a', 'b', 'c', 'fresh', 'another-fresh'])


def test_every_finite_set_against_oracle():
  base = frozenset({'a', 'b', 'c'})
  check_family([Finite(base, members) for members in powerset(base)], list(base))


def test_integer_unions_with_unbounded_ends_against_oracle():
  intervals = [
    IntegerInterval(lower, upper)
    for lower in (None, -1, 0, 1)
    for upper in (-1, 0, 1, None)
    if lower is None or upper is None or lower <= upper
  ]
  sets = list(
    dict.fromkeys(
      [
        Integers(),
        *(Integers((interval,)) for interval in intervals),
        *(Integers(pair) for pair in combinations_with_replacement(intervals, 2)),
      ]
    )
  )
  check_family(sets, list(range(-3, 4)) + [-1000000, 1000000])


def test_lattice_interval_unions_against_oracle():
  base = frozenset({'a', 'b', 'c'})
  subsets = powerset(base)
  intervals = [
    LatticeInterval(lower, upper) for lower, upper in product(subsets, repeat=2) if lower <= upper
  ]
  sets = list(
    dict.fromkeys(
      [
        Subsets(base),
        *(Subsets(base, (interval,)) for interval in intervals),
        *(Subsets(base, pair) for pair in combinations_with_replacement(intervals, 2)),
      ]
    )
  )
  check_family(sets, list(subsets))


def test_empty_subset_is_a_value_and_not_an_empty_value_set():
  family = Subsets(frozenset({'b'}))
  assert not family.singleton(frozenset()).is_empty
  assert family.singleton(frozenset()).contains(frozenset())
  assert family.is_empty


def test_model_value_set_examples():
  base = frozenset({'b1', 'b2', 'b3', 'b4'})
  subsets_of_three = Subsets(base, (LatticeInterval(frozenset(), base - {'b4'}),))
  supersets_of_three = Subsets(base, (LatticeInterval(frozenset({'b3'}), base),))
  assert {
    value
    for value in powerset(base)
    if subsets_of_three.intersection(supersets_of_three).contains(value)
  } == {
    frozenset({'b3'}),
    frozenset({'b1', 'b3'}),
    frozenset({'b2', 'b3'}),
    frozenset({'b1', 'b2', 'b3'}),
  }
  assert Integers((IntegerInterval(10, 20),)).difference(
    Integers((IntegerInterval(15, 30),))
  ) == Integers((IntegerInterval(10, 14),))


@pytest.mark.parametrize('value', [True, '1', frozenset()])
def test_integer_membership_does_not_coerce(value):
  assert not Integers((IntegerInterval(),)).contains(value)


def test_flags_do_not_accept_integer_one():
  flags = Finite(frozenset({True}), frozenset({True}))
  assert not flags.contains(1)
  assert flags.contains(True)


def test_incompatible_families_and_unbounded_subset_base_are_refused():
  with pytest.raises(ValueError, match='incompatible'):
    Strings().intersection(Integers())
  with pytest.raises(ValueError, match='incompatible'):
    Subsets(frozenset({'a'})).difference(Subsets(frozenset({'b'})))
  with pytest.raises(ValueError, match='finite'):
    Subsets(Strings(cofinite=True))  # type: ignore[arg-type]
  with pytest.raises(ValueError, match='reversed'):
    IntegerInterval(5, 1)


def test_finite_flags_and_integers_are_distinct_domains():
  with pytest.raises(ValueError, match='outside base'):
    Finite(frozenset({True}), frozenset({1}))
  with pytest.raises(ValueError, match='incompatible'):
    Finite(frozenset({True}), frozenset()).intersection(Finite(frozenset({1}), frozenset()))


def test_integer_string_sum_against_oracle():
  base = frozenset({'none', 'auto'})
  integers = [Integers(), Integers().universe(), Integers((IntegerInterval(0, 2),))]
  sets = [
    Scalars((numbers, Finite(base, names))) for numbers, names in product(integers, powerset(base))
  ]
  check_family(sets, [-1000, -1, 0, 1, 2, 3, 1000, 'none', 'auto'])
  assert not sets[-1].contains(True)
  assert not sets[-1].contains('other')
  with pytest.raises(ValueError):
    sets[-1].singleton(True)


def test_string_base_grammar_stays_closed_under_operations():
  domain = Strings(
    cofinite=True, grammar=StringGrammar(frozenset('abcfrnesh'), frozenset('abcfrnesh'))
  )
  left, right = domain.singleton('a'), domain.singleton('b')
  check_family([domain, left, right, domain.difference(left)], ['a', 'b', 'fresh'])
  assert not domain.contains('a1')
  assert not domain.difference(left).contains('a1')
  with pytest.raises(ValueError, match='grammar'):
    domain.singleton('a1')
  with pytest.raises(ValueError, match='incompatible'):
    domain.intersection(Strings(cofinite=True))


def test_string_grammars_require_an_infinite_base():
  for initial, remaining in ((frozenset(), frozenset('ab')), (frozenset('ab'), frozenset())):
    with pytest.raises(ValueError, match='non-empty'):
      StringGrammar(initial, remaining)
  with pytest.raises(ValueError, match='infinite'):
    Strings(cofinite=True, grammar='a|b')  # pyright: ignore[reportArgumentType]
