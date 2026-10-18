"""Descriptor rectangles and exact inclusion of their finite unions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .values import Value, ValueSet


@dataclass(frozen=True)
class Descriptor:
  type: str
  values: Mapping[str, Value] = field(default_factory=dict)

  def __post_init__(self) -> None:
    object.__setattr__(self, 'values', MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class _State:
  values: ValueSet | None = None
  unset: bool = True
  set_allowed: bool = True

  @property
  def is_empty(self) -> bool:
    return not self.unset and (
      not self.set_allowed or self.values is not None and self.values.is_empty
    )

  def intersection(self, other: _State) -> _State:
    values = (
      other.values
      if self.values is None
      else self.values
      if other.values is None
      else self.values.intersection(other.values)
    )
    return _State(values, self.unset and other.unset, self.set_allowed and other.set_allowed)

  def difference(self, other: _State) -> _State:
    if not other.set_allowed:
      return _State(self.values, self.unset and not other.unset, self.set_allowed)
    if other.values is None:
      return _State(None, self.unset and not other.unset, False)
    values = (self.values if self.values is not None else other.values.universe()).difference(
      other.values
    )
    return _State(values, self.unset and not other.unset, self.set_allowed)


@dataclass(frozen=True)
class Pattern:
  type: str
  values: Mapping[str, ValueSet] = field(default_factory=dict)
  required: frozenset[str] = frozenset()
  unset: frozenset[str] = frozenset()

  def __post_init__(self) -> None:
    object.__setattr__(self, 'values', MappingProxyType(dict(self.values)))

  @property
  def options(self) -> frozenset[str]:
    return frozenset(self.values) | self.required | self.unset

  def _state(self, option: str) -> _State:
    return _State(self.values.get(option), option not in self.required, option not in self.unset)

  @classmethod
  def _from_states(cls, type: str, states: Mapping[str, _State]) -> Pattern:
    return cls(
      type,
      {option: state.values for option, state in states.items() if state.values is not None},
      frozenset(option for option, state in states.items() if not state.unset),
      frozenset(option for option, state in states.items() if not state.set_allowed),
    )

  @property
  def is_empty(self) -> bool:
    return any(self._state(option).is_empty for option in self.options)

  def matches(self, descriptor: Descriptor, *, prepare: bool = False) -> bool:
    if descriptor.type != self.type or (not prepare and self.is_empty):
      return False
    for option in self.options:
      state = self._state(option)
      if option not in descriptor.values:
        if not prepare and not state.unset:
          return False
      elif (
        not state.set_allowed
        or state.values is not None
        and not state.values.contains(descriptor.values[option])
      ):
        return False
    return True

  def meet(self, other: Pattern) -> Pattern | None:
    if self.type != other.type:
      return None
    result = self._from_states(
      self.type,
      {
        option: self._state(option).intersection(other._state(option))
        for option in self.options | other.options
      },
    )
    return result

  def difference(self, other: Pattern) -> tuple[Pattern, ...]:
    if self.is_empty:
      return ()
    if self.type != other.type:
      return (self,)
    remaining = {option: self._state(option) for option in self.options | other.options}
    pieces = []
    for option in sorted(other.options):
      state = remaining[option]
      outside = state.difference(other._state(option))
      if not outside.is_empty:
        pieces.append(self._from_states(self.type, {**remaining, option: outside}))
      inside = state.intersection(other._state(option))
      if inside.is_empty:
        return tuple(pieces)
      remaining[option] = inside
    return tuple(pieces)


@dataclass(frozen=True)
class PatternSet:
  patterns: tuple[Pattern, ...] = ()

  @classmethod
  def all(cls, *types: str) -> PatternSet:
    return cls(tuple(Pattern(type) for type in types))

  @property
  def is_empty(self) -> bool:
    return all(pattern.is_empty for pattern in self.patterns)

  def matches(self, descriptor: Descriptor, *, prepare: bool = False) -> bool:
    return any(pattern.matches(descriptor, prepare=prepare) for pattern in self.patterns)

  def join(self, other: PatternSet) -> PatternSet:
    return PatternSet(self.patterns + other.patterns)

  def meet(self, other: PatternSet) -> PatternSet:
    return PatternSet(
      tuple(
        overlap
        for left in self.patterns
        for right in other.patterns
        if (overlap := left.meet(right)) is not None
      )
    )

  def within(
    self, other: PatternSet, domains: Mapping[str, Mapping[str, ValueSet]] | None = None
  ) -> bool:
    """Decide inclusion over declared domains, or the families' full universes."""
    patterns = self.patterns
    if domains is not None:
      patterns = tuple(
        pattern.meet(Pattern(pattern.type, domains[pattern.type])) for pattern in patterns
      )
    remaining = tuple(
      pattern for pattern in patterns if pattern is not None and not pattern.is_empty
    )
    for cut in other.patterns:
      remaining = tuple(piece for pattern in remaining for piece in pattern.difference(cut))
    return not remaining
