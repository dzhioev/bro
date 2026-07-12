"""first-class conditions over typed variables.

A condition is an immutable predicate tree built at declaration time —
`var('harness') == 'claude'` for equality, `var('creds').contains('openai')`
for membership — and evaluated later against a `Variables` mapping, so a
class-level declaration stays an import-time constant while its truth is
decided by whatever surface holds the facts. `base.template` directives lower
onto the same objects, so text conditions and code-built ones share one
evaluator and one fail-fast semantics; `when` / `iff` / `select` gate entries
of declarative lists. The full semantics — variables, fail-fast rules, facts,
consuming surfaces — live in `reference/conditions.md`.

Membership is spelled as the `contains` method, not Python's `in` operator:
the interpreter coerces `__contains__`'s result to bool, which would evaluate
the condition at declaration time, before the facts exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Optional, cast


class ConditionError(ValueError):
  """an unknown variable, a type-mismatched comparison, a domain/universe
  violation, or an exhausted `iff`."""


@dataclass(frozen=True)
class StringVariable:
  """a string-valued variable. `domain`, when given, is the closed set of legal
  comparands: comparing the variable against a literal outside it raises."""

  value: str
  domain: Optional[frozenset[str]] = None


@dataclass(frozen=True)
class SetVariable:
  """a set-valued variable for membership tests. `universe`, when given, is the
  closed set of names that may be tested: testing a name outside it raises.
  `members` is either the materialized set or a membership predicate — the lazy
  form for sets whose membership is expensive to probe (only names a condition
  actually tests get probed)."""

  members: frozenset[str] | Callable[[str], bool]
  universe: Optional[frozenset[str]] = None


type Variables = Mapping[str, StringVariable | SetVariable | bool]


@dataclass(frozen=True, eq=False)
class Variable:
  """a by-name variable reference, resolved against the `Variables` mapping at
  evaluation time — the code spelling of a directive's `#name`."""

  name: str

  def __eq__(self, other: object) -> Condition:  # pyright: ignore[reportIncompatibleMethodOverride]
    if not isinstance(other, (Variable, str)):
      raise ConditionError(
        f'cannot compare #{self.name} against {type(other).__name__}; '
        'operands are variable references and string literals'
      )
    return Equals(self, other)

  def __ne__(self, other: object) -> Condition:  # pyright: ignore[reportIncompatibleMethodOverride]
    raise ConditionError('conditions have no != form; compare against the intended value with ==')

  def __hash__(self) -> int:
    return hash(self.name)

  def contains(self, element: Variable | str) -> Condition:
    """membership condition `element` in this set-valued variable."""
    return Contains(element, self)

  def __contains__(self, element: object) -> bool:
    # the interpreter coerces this hook's result to bool, so `in` can never
    # yield a deferred Condition — it would evaluate at declaration time.
    raise ConditionError(
      f"'in' cannot build a condition; use #{self.name}.contains(<element>) instead"
    )

  def __str__(self) -> str:
    return f'#{self.name}'


# a condition operand: a variable reference or a string literal.
type Operand = Variable | str

# what an operand resolves to: a variable's typed value or the literal itself.
type _Value = StringVariable | SetVariable | bool | str


def var(name: str) -> Variable:
  return Variable(name)


class Condition(ABC):
  """a deferred boolean predicate over typed variables. `str()` renders the
  canonical directive form (`#harness = claude`), used in error messages."""

  @abstractmethod
  def evaluate(self, variables: Variables) -> bool:
    """resolve the operands against `variables` and decide the condition,
    fail-fast on any semantic violation (see the module docstring)."""

  @abstractmethod
  def __str__(self) -> str: ...

  def __bool__(self) -> bool:
    # a condition carries no truth at declaration time; whoever lands here
    # meant to defer it (`when`/`iff`) or to evaluate it against facts.
    raise ConditionError(
      f'condition {str(self)!r} has no truth value until evaluated against facts'
    )


def _operand_text(operand: Operand) -> str:
  return str(operand) if isinstance(operand, Variable) else operand


def _resolve(operand: Operand, variables: Variables, condition: Condition) -> _Value:
  if isinstance(operand, str):
    return operand
  if operand.name not in variables:
    known = ', '.join(sorted(variables))
    raise ConditionError(f'unknown variable #{operand.name} in {str(condition)!r}; known: {known}')
  return variables[operand.name]


def _decide(condition: Condition | bool, variables: Variables) -> bool:
  """the truth of a deferred condition or a declaration-time bool constant."""
  return condition if isinstance(condition, bool) else condition.evaluate(variables)


@dataclass(frozen=True)
class Equals(Condition):
  left: Operand
  right: Operand

  def evaluate(self, variables: Variables) -> bool:
    left = _resolve(self.left, variables, self)
    right = _resolve(self.right, variables, self)
    if isinstance(left, SetVariable) or isinstance(right, SetVariable):
      raise ConditionError(f'sets cannot be compared with = in {str(self)!r}; use contains')
    if isinstance(left, bool) or isinstance(right, bool):
      if not (isinstance(left, bool) and isinstance(right, bool)):
        raise ConditionError(f'boolean compared against a string in {str(self)!r}')
      return left == right
    return self._string_value(left, other=right) == self._string_value(right, other=left)

  def _string_value(self, operand: StringVariable | str, other: StringVariable | str) -> str:
    # a literal compared against a domain-closed variable must belong to the
    # domain — a misspelled literal is a bug, not a false comparison.
    if isinstance(operand, str):
      if isinstance(other, StringVariable) and other.domain is not None:
        if operand not in other.domain:
          raise ConditionError(
            f'literal {operand!r} outside the domain of its comparand in {str(self)!r}; '
            f'domain: {", ".join(sorted(other.domain))}'
          )
      return operand
    return operand.value

  def __str__(self) -> str:
    return f'{_operand_text(self.left)} = {_operand_text(self.right)}'


@dataclass(frozen=True)
class Contains(Condition):
  element: Operand
  container: Operand

  def evaluate(self, variables: Variables) -> bool:
    element = _resolve(self.element, variables, self)
    container = _resolve(self.container, variables, self)
    if not isinstance(container, SetVariable):
      raise ConditionError(f'the container in {str(self)!r} is not a set')
    if isinstance(element, StringVariable):
      element = element.value
    if not isinstance(element, str):
      raise ConditionError(f'the element in {str(self)!r} is not a string')
    if container.universe is not None and element not in container.universe:
      raise ConditionError(
        f'{element!r} outside the set universe in {str(self)!r}; '
        f'universe: {", ".join(sorted(container.universe))}'
      )
    if callable(container.members):
      return container.members(element)
    return element in container.members

  def __str__(self) -> str:
    return f'{_operand_text(self.container)} contains {_operand_text(self.element)}'


@dataclass(frozen=True)
class When[T]:
  """a declarative-list entry included only when its condition holds; see `when`."""

  condition: Condition | bool
  item: T

  def resolve(self, variables: Variables) -> tuple[T, ...]:
    return (self.item,) if _decide(self.condition, variables) else ()


@dataclass(frozen=True)
class Iff[T]:
  """a declarative-list entry choosing among condition-gated alternatives; see `iff`."""

  branches: tuple[tuple[Condition | bool, T], ...]
  # None is "no else"; an explicit else item rides in a 1-tuple so that the
  # item itself may be any value.
  otherwise: Optional[tuple[T]]

  def resolve(self, variables: Variables) -> tuple[T, ...]:
    # every branch condition is evaluated — a typo fails every selection, not
    # just the unlucky one — and the first that holds wins.
    chosen: Optional[tuple[T]] = None
    for condition, item in self.branches:
      if _decide(condition, variables) and chosen is None:
        chosen = (item,)
    if chosen is not None:
      return chosen
    if self.otherwise is not None:
      return self.otherwise
    conditions = ', '.join(str(condition) for condition, _ in self.branches)
    raise ConditionError(f'no condition of iff({conditions}) holds and there is no else item')


# what a declarative list may hold: plain items, `when`-gated items, and
# `iff` alternative groups. `select` resolves a list of these.
type Entry[T] = T | When[T] | Iff[T]


def when[T](condition: Condition | bool, item: T) -> When[T]:
  """wrap `item` so it enters a declarative list only when `condition` holds —
  reads "when <condition> add <item>"; absence is meaningful, not an error. a
  plain `bool` is a declaration-time constant, fine for genuinely static
  predicates; anything fact-dependent uses a `Condition` and defers to
  `select` time."""
  return When(condition, item)


def iff[T](condition: Condition | bool, item: T, *rest: Condition | bool | T) -> Iff[T]:
  """exhaustive choice among condition-gated alternatives: `(condition, item)`
  pairs flattened in order, optionally followed by one trailing else item —
  `iff(c1, a1, c2, a2, e)`. the first holding condition selects its item; when
  none holds, the else item is selected, and with no else the selection
  raises — the implicit exhaustiveness guard."""
  arguments: list[object] = [condition, item, *rest]
  otherwise: Optional[tuple[object]] = None
  if len(arguments) % 2 == 1:
    otherwise = (arguments.pop(),)
  branches = []
  for gate, action in zip(arguments[0::2], arguments[1::2], strict=True):
    if not isinstance(gate, (Condition, bool)):
      raise ConditionError(
        f'iff arguments are (condition, item) pairs plus an optional trailing '
        f'else item; got {type(gate).__name__} in a condition position'
      )
    branches.append((gate, action))
  return cast(Iff[T], Iff(tuple(branches), otherwise))


def select[T](entries: Iterable[Entry[T]], variables: Variables) -> list[T]:
  """a declarative list resolved against `variables`: plain entries pass
  through, `when` entries are included when they hold, `iff` entries yield
  their selected alternative."""

  def resolve(entry: Entry[T]) -> tuple[T, ...]:
    if isinstance(entry, (When, Iff)):
      return entry.resolve(variables)
    return (entry,)

  return [item for entry in entries for item in resolve(entry)]
