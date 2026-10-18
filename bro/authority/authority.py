"""Pointwise authority joins and cap meets over symbolic atom regions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .patterns import Descriptor, PatternSet
from .values import Value, ValueSet


@dataclass(frozen=True)
class Atom:
  type: str
  option: str | None = None
  value: Value | None = None

  def __post_init__(self) -> None:
    if (self.option is None) != (self.value is None):
      raise ValueError('option atoms require both an option and a value')


@dataclass(frozen=True)
class Permission:
  type: str
  option: str | None = None
  values: ValueSet | None = None

  def __post_init__(self) -> None:
    if (self.option is None) != (self.values is None):
      raise ValueError('option permissions require an option and a value set')

  def contains(self, atom: Atom) -> bool:
    return (
      self.type == atom.type
      and self.option == atom.option
      and (self.values is None or self.values.contains(atom.value))
    )

  def intersection(self, other: Permission) -> Permission | None:
    if (self.type, self.option) != (other.type, other.option):
      return None
    if self.values is None:
      return self
    assert other.values is not None
    values = self.values.intersection(other.values)
    return None if values.is_empty else Permission(self.type, self.option, values)

  def difference(self, other: Permission) -> tuple[Permission, ...]:
    if (self.type, self.option) != (other.type, other.option):
      return (self,)
    if self.values is None:
      return ()
    assert other.values is not None
    values = self.values.difference(other.values)
    return () if values.is_empty else (Permission(self.type, self.option, values),)


@dataclass(frozen=True)
class Pair:
  use: PatternSet = PatternSet()
  give: PatternSet = PatternSet()

  @classmethod
  def top(cls, type: str, types: Iterable[str]) -> Pair:
    return cls(PatternSet.all(type), PatternSet.all(*types))

  @property
  def is_empty(self) -> bool:
    return self.use.is_empty and self.give.is_empty

  def join(self, other: Pair) -> Pair:
    return Pair(self.use.join(other.use), self.give.join(other.give))

  def meet(self, other: Pair) -> Pair:
    return Pair(self.use.meet(other.use), self.give.meet(other.give))


@dataclass(frozen=True)
class Entry:
  permission: Permission
  pair: Pair


@dataclass(frozen=True)
class LazyEntry:
  """A fixed permission with a pure, region-constant pair for a checked start.

  The resolver must return the same pair for every atom of the permission.
  """

  permission: Permission
  resolve: Callable[[Descriptor], Pair]


@dataclass(frozen=True)
class Authority:
  entries: tuple[Entry | LazyEntry, ...] = ()
  operation: str = 'join'
  operands: tuple[Authority, ...] = ()

  def __post_init__(self) -> None:
    if self.operation not in ('join', 'meet', 'top'):
      raise ValueError('unknown authority operation')
    if self.operation == 'top' and (self.entries or self.operands):
      raise ValueError('top has no entries or operands')

  @classmethod
  def top(cls) -> Authority:
    return cls(operation='top')

  def join(self, other: Authority) -> Authority:
    return Authority(operands=(self, other))

  def meet(self, other: Authority) -> Authority:
    return Authority(operation='meet', operands=(self, other))

  def permissions(self) -> tuple[Permission, ...]:
    return tuple(entry.permission for entry in self.entries) + tuple(
      permission for operand in self.operands for permission in operand.permissions()
    )

  def evaluate_region(
    self,
    region: Permission,
    descriptor: Descriptor,
    types: Iterable[str],
    validate_pair: Callable[[Permission, Pair], None] | None = None,
  ) -> Pair:
    """Evaluate a region split at every permission boundary of this expression."""
    types = tuple(types)
    top = Pair.top(region.type, types)
    result = top if self.operation in ('meet', 'top') else Pair()
    combine = Pair.meet if self.operation == 'meet' else Pair.join
    for entry in self.entries:
      overlap = entry.permission.intersection(region)
      if overlap is None:
        continue
      if region.difference(entry.permission):
        raise ValueError('region crosses an authority permission boundary')
      pair = entry.resolve(descriptor) if isinstance(entry, LazyEntry) else entry.pair
      if validate_pair is not None:
        validate_pair(entry.permission, pair)
      result = combine(result, pair)
    for operand in self.operands:
      result = combine(result, operand.evaluate_region(region, descriptor, types, validate_pair))
    return result

  def evaluate(
    self,
    atom: Atom,
    descriptor: Descriptor,
    types: Iterable[str],
    validate_pair: Callable[[Permission, Pair], None] | None = None,
  ) -> Pair:
    types = tuple(types)
    if atom.option is None:
      region = Permission(atom.type)
    else:
      family = next(
        (
          permission.values
          for permission in self.permissions()
          if permission.type == atom.type and permission.option == atom.option
        ),
        None,
      )
      if family is None:
        return self._evaluate_unmentioned(atom, types)
      assert atom.value is not None
      region = Permission(atom.type, atom.option, family.singleton(atom.value))
    return self.evaluate_region(region, descriptor, types, validate_pair)

  def _evaluate_unmentioned(self, atom: Atom, types: Iterable[str]) -> Pair:
    result = Pair.top(atom.type, types) if self.operation in ('meet', 'top') else Pair()
    combine = Pair.meet if self.operation == 'meet' else Pair.join
    for operand in self.operands:
      result = combine(result, operand._evaluate_unmentioned(atom, types))
    return result


class Cap(Authority):
  def __init__(self, entries: tuple[Entry | LazyEntry, ...] = ()):
    super().__init__(entries, operation='meet')


def partition(permissions: Iterable[Permission]) -> tuple[Permission, ...]:
  regions: list[Permission] = []
  for permission in permissions:
    if permission.values is not None and permission.values.is_empty:
      continue
    uncovered = (permission,)
    split = []
    for region in regions:
      overlap = region.intersection(permission)
      split.extend(region.difference(permission))
      if overlap is not None:
        split.append(overlap)
      uncovered = tuple(piece for item in uncovered for piece in item.difference(region))
    regions = split + list(uncovered)
  return tuple(regions)
