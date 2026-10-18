"""Closed symbolic families of option values."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Self

type Value = str | int | bool | frozenset[str]


class ValueSet(ABC):
  @abstractmethod
  def intersection(self, other: ValueSet) -> Self: ...

  @abstractmethod
  def difference(self, other: ValueSet) -> Self: ...

  @abstractmethod
  def contains(self, value: object) -> bool: ...

  @abstractmethod
  def universe(self) -> Self: ...

  @abstractmethod
  def singleton(self, value: Value) -> Self: ...

  @property
  @abstractmethod
  def is_empty(self) -> bool: ...

  def empty(self) -> Self:
    return self.difference(self)

  def compatible(self, other: ValueSet) -> bool:
    return type(self) is type(other) and self.universe() == other.universe()

  def _check(self, other: ValueSet) -> None:
    if not self.compatible(other):
      raise ValueError('incompatible value-set families or bases')


@dataclass(frozen=True)
class Strings(ValueSet):
  members: frozenset[str] = frozenset()
  cofinite: bool = False

  def __post_init__(self) -> None:
    if not isinstance(self.members, frozenset) or any(
      type(value) is not str for value in self.members
    ):
      raise ValueError('string members must be a frozenset of strings')

  def intersection(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Strings)
    if self.cofinite and other.cofinite:
      return type(self)(self.members | other.members, True)
    if self.cofinite:
      return type(self)(other.members - self.members)
    if other.cofinite:
      return type(self)(self.members - other.members)
    return type(self)(self.members & other.members)

  def difference(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Strings)
    return self.intersection(type(self)(other.members, not other.cofinite))

  def contains(self, value: object) -> bool:
    return type(value) is str and (
      (value not in self.members) if self.cofinite else value in self.members
    )

  def universe(self) -> Self:
    return type(self)(frozenset(), True)

  def singleton(self, value: Value) -> Self:
    if type(value) is not str:
      raise ValueError('expected a string')
    return type(self)(frozenset({value}))

  @property
  def is_empty(self) -> bool:
    return not self.cofinite and not self.members


@dataclass(frozen=True)
class Finite(ValueSet):
  base: frozenset[Value]
  members: frozenset[Value]

  def __post_init__(self) -> None:
    if not isinstance(self.base, frozenset) or not isinstance(self.members, frozenset):
      raise ValueError('finite sets require frozensets')
    if any(
      not any(type(member) is type(value) and member == value for value in self.base)
      for member in self.members
    ):
      raise ValueError('finite members outside base')
    if any(type(value) not in (str, int, bool) for value in self.base):
      raise ValueError('finite scalar base requires strings, integers, or flags')
    if len({type(value) for value in self.base}) > 1:
      raise ValueError('finite base must have one scalar type')

  def compatible(self, other: ValueSet) -> bool:
    return (
      isinstance(other, Finite)
      and self.base == other.base
      and {type(value) for value in self.base} == {type(value) for value in other.base}
    )

  def intersection(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Finite)
    return type(self)(self.base, self.members & other.members)

  def difference(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Finite)
    return type(self)(self.base, self.members - other.members)

  def contains(self, value: object) -> bool:
    return any(type(value) is type(member) and value == member for member in self.members)

  def universe(self) -> Self:
    return type(self)(self.base, self.base)

  def singleton(self, value: Value) -> Self:
    if not self.universe().contains(value):
      raise ValueError('value outside finite base')
    return type(self)(self.base, frozenset({value}))

  @property
  def is_empty(self) -> bool:
    return not self.members


@dataclass(frozen=True)
class IntegerInterval:
  lower: int | None = None
  upper: int | None = None

  def __post_init__(self) -> None:
    if any(value is not None and type(value) is not int for value in (self.lower, self.upper)):
      raise ValueError('interval ends must be integers or unbounded')
    if self.lower is not None and self.upper is not None and self.lower > self.upper:
      raise ValueError('reversed integer interval')

  def intersection(self, other: IntegerInterval) -> IntegerInterval | None:
    lower = max((value for value in (self.lower, other.lower) if value is not None), default=None)
    upper = min((value for value in (self.upper, other.upper) if value is not None), default=None)
    if lower is not None and upper is not None and lower > upper:
      return None
    return IntegerInterval(lower, upper)


@dataclass(frozen=True)
class Integers(ValueSet):
  intervals: tuple[IntegerInterval, ...] = ()

  def __post_init__(self) -> None:
    result: list[IntegerInterval] = []
    for interval in sorted(
      self.intervals, key=lambda item: (item.lower is not None, item.lower or 0)
    ):
      if result:
        previous = result[-1]
        if previous.upper is None or interval.lower is None or interval.lower <= previous.upper + 1:
          upper = (
            None
            if previous.upper is None or interval.upper is None
            else max(previous.upper, interval.upper)
          )
          result[-1] = IntegerInterval(previous.lower, upper)
          continue
      result.append(interval)
    object.__setattr__(self, 'intervals', tuple(result))

  def intersection(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Integers)
    return type(self)(
      tuple(
        overlap
        for left in self.intervals
        for right in other.intervals
        if (overlap := left.intersection(right)) is not None
      )
    )

  def difference(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Integers)
    remaining = list(self.intervals)
    for cut in other.intervals:
      pieces = []
      for interval in remaining:
        overlap = interval.intersection(cut)
        if overlap is None:
          pieces.append(interval)
          continue
        if overlap.lower is not None and (interval.lower is None or interval.lower < overlap.lower):
          pieces.append(IntegerInterval(interval.lower, overlap.lower - 1))
        if overlap.upper is not None and (interval.upper is None or interval.upper > overlap.upper):
          pieces.append(IntegerInterval(overlap.upper + 1, interval.upper))
      remaining = pieces
    return type(self)(tuple(remaining))

  def contains(self, value: object) -> bool:
    return type(value) is int and any(
      (interval.lower is None or interval.lower <= value)
      and (interval.upper is None or value <= interval.upper)
      for interval in self.intervals
    )

  def universe(self) -> Self:
    return type(self)((IntegerInterval(),))

  def singleton(self, value: Value) -> Self:
    if type(value) is not int:
      raise ValueError('expected an integer')
    return type(self)((IntegerInterval(value, value),))

  @property
  def is_empty(self) -> bool:
    return not self.intervals


@dataclass(frozen=True)
class LatticeInterval:
  lower: frozenset[str]
  upper: frozenset[str]

  def __post_init__(self) -> None:
    if not isinstance(self.lower, frozenset) or not isinstance(self.upper, frozenset):
      raise ValueError('lattice ends require finite frozensets')
    if any(type(value) is not str for value in self.upper):
      raise ValueError('lattice ends require strings')
    if not self.lower <= self.upper:
      raise ValueError('reversed lattice interval')

  def intersection(self, other: LatticeInterval) -> LatticeInterval | None:
    lower, upper = self.lower | other.lower, self.upper & other.upper
    return LatticeInterval(lower, upper) if lower <= upper else None


@dataclass(frozen=True)
class Subsets(ValueSet):
  base: frozenset[str]
  intervals: tuple[LatticeInterval, ...] = ()

  def __post_init__(self) -> None:
    if not isinstance(self.base, frozenset) or any(type(value) is not str for value in self.base):
      raise ValueError('subset options require a finite frozenset of strings')
    result = []
    for interval in self.intervals:
      if not interval.upper <= self.base:
        raise ValueError('lattice interval outside base')
      if interval not in result:
        result.append(interval)
    result = [
      item
      for item in result
      if not any(
        other != item and other.lower <= item.lower and item.upper <= other.upper
        for other in result
      )
    ]
    object.__setattr__(
      self,
      'intervals',
      tuple(sorted(result, key=lambda item: (sorted(item.lower), sorted(item.upper)))),
    )

  def intersection(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Subsets)
    return type(self)(
      self.base,
      tuple(
        overlap
        for left in self.intervals
        for right in other.intervals
        if (overlap := left.intersection(right)) is not None
      ),
    )

  def difference(self, other: ValueSet) -> Self:
    self._check(other)
    assert isinstance(other, Subsets)
    remaining = list(self.intervals)
    for cut in other.intervals:
      pieces = []
      for interval in remaining:
        overlap = interval.intersection(cut)
        if overlap is None:
          pieces.append(interval)
          continue
        current = interval
        for member in sorted(overlap.lower - interval.lower):
          pieces.append(LatticeInterval(current.lower, current.upper - {member}))
          current = LatticeInterval(current.lower | {member}, current.upper)
        for member in sorted(interval.upper - overlap.upper):
          pieces.append(LatticeInterval(current.lower | {member}, current.upper))
          current = LatticeInterval(current.lower, current.upper - {member})
      remaining = pieces
    return type(self)(self.base, tuple(remaining))

  def contains(self, value: object) -> bool:
    return (
      isinstance(value, frozenset)
      and value <= self.base
      and any(interval.lower <= value <= interval.upper for interval in self.intervals)
    )

  def universe(self) -> Self:
    return type(self)(self.base, (LatticeInterval(frozenset(), self.base),))

  def singleton(self, value: Value) -> Self:
    if not isinstance(value, frozenset) or not value <= self.base:
      raise ValueError('value outside subset base')
    return type(self)(self.base, (LatticeInterval(value, value),))

  @property
  def is_empty(self) -> bool:
    return not self.intervals
