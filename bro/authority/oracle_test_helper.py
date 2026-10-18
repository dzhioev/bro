from collections.abc import Iterable
from itertools import combinations, product

from .authority import Atom, Authority, LazyEntry
from .patterns import Descriptor, Pattern, PatternSet
from .values import Finite, Integers, Scalars, Strings, Subsets, Value, ValueSet


def powerset[Item](values: Iterable[Item]) -> list[frozenset[Item]]:
  sequence = tuple(values)
  return [
    frozenset(items)
    for count in range(len(sequence) + 1)
    for items in combinations(sequence, count)
  ]


def member(values: ValueSet, value: Value) -> bool:
  if isinstance(values, Strings):
    return (
      type(value) is str
      and (
        values.grammar is None
        or (
          values.grammar.empty
          if not value
          else value[0] in values.grammar.initial and set(value[1:]) <= values.grammar.remaining
        )
      )
      and ((value in values.members) != values.cofinite)
    )
  if isinstance(values, Finite):
    return any(type(value) is type(item) and item == value for item in values.members)
  if isinstance(values, Integers):
    return type(value) is int and any(
      (interval.lower is None or value >= interval.lower)
      and (interval.upper is None or value <= interval.upper)
      for interval in values.intervals
    )
  if isinstance(values, Scalars):
    return any(member(component, value) for component in values.components)
  if isinstance(values, Subsets):
    return isinstance(value, frozenset) and any(
      interval.lower <= value and value <= interval.upper for interval in values.intervals
    )
  raise TypeError(values)


def matches(pattern: Pattern, descriptor: Descriptor, *, prepare: bool = False) -> bool:
  return (
    pattern.type == descriptor.type
    and (prepare or pattern.required <= descriptor.values.keys())
    and all(
      name not in descriptor.values or member(values, descriptor.values[name])
      for name, values in pattern.values.items()
    )
    and not pattern.unset & descriptor.values.keys()
  )


def pattern_matches(patterns: PatternSet, descriptor: Descriptor, *, prepare: bool = False) -> bool:
  return any(matches(pattern, descriptor, prepare=prepare) for pattern in patterns.patterns)


def descriptors(type: str, domains: dict[str, list[Value]]) -> list[Descriptor]:
  return [
    Descriptor(
      type, {name: value for name, value in zip(domains, row, strict=True) if value is not None}
    )
    for row in product(*([None, *values] for values in domains.values()))
  ]


def evaluate(
  authority: Authority, atom: Atom, start: Descriptor, probe: Descriptor, *, give: bool
) -> bool:
  results = []
  for entry in authority.entries:
    permission = entry.permission
    if (
      permission.type == atom.type
      and permission.option == atom.option
      and (
        permission.values is None
        or atom.value is not None
        and member(permission.values, atom.value)
      )
    ):
      pair = entry.resolve(start) if isinstance(entry, LazyEntry) else entry.pair
      results.append(pattern_matches(pair.give if give else pair.use, probe))
  results.extend(evaluate(operand, atom, start, probe, give=give) for operand in authority.operands)
  if authority.operation == 'top':
    return give or atom.type == probe.type
  return all(results) if authority.operation == 'meet' else any(results)
