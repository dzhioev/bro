"""conditional template engine for static agent-facing text (tool descriptions,
skill bodies).

Grammar:

  template  := (text | directive)*
  directive := '{{if' condition '}}' template
               ('{{elif' condition '}}' template)*
               ('{{else}}' template)?
               '{{endif}}'
             | '{{assert' condition '}}'
  condition := value ('=' | '∈') value
  value     := '#' name | name            name: [A-Za-z0-9_-]+

A `#name` value references a variable from the render call; a bare name is a
string literal. `=` compares two strings or two booleans; `∈` tests a string's
membership in a set variable. `{{assert}}` renders to nothing when its condition
holds and raises when it does not — the guard for a branch that must only be
reached under a known state (e.g. an `{{else}}` that assumes the one other value
of a variable). Blocks nest; conditions in non-taken branches are still
evaluated (a typo fails every render, not just the unlucky branch), while
`{{assert}}` directives in non-taken branches do not fire.

Variables are typed: `StringVariable` (an optional `domain` makes comparing
against a literal outside it an error), `SetVariable` (an optional `universe`
makes testing a name outside it an error), or a plain `bool`. `true` and `false`
are built-in boolean variables. Domain/universe checks exist so a misspelled
literal raises instead of silently rendering one branch forever.

Only `{{` groups whose first token is a directive keyword are parsed; any other
`{{…}}` is literal text, so braces in code samples survive rendering.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Optional

_NAME = r'[A-Za-z0-9_-]+'

_DIRECTIVE_RE = re.compile(
  r'\{\{\s*(?P<keyword>if|elif|else|endif|assert)\b(?P<argument>[^}]*)\}\}'
)

_CONDITION_RE = re.compile(
  rf'^\s*(?P<left>\#?{_NAME})\s*(?P<operator>=|∈)\s*(?P<right>\#?{_NAME})\s*$'
)


class TemplateError(ValueError):
  """a malformed template, an unknown variable, a type-mismatched condition, or
  a failed `{{assert}}`."""


@dataclass(frozen=True)
class StringVariable:
  """a string-valued variable. `domain`, when given, is the closed set of legal
  comparands: comparing the variable against a literal outside it raises."""

  value: str
  domain: Optional[frozenset[str]] = None


@dataclass(frozen=True)
class SetVariable:
  """a set-valued variable for `∈` tests. `universe`, when given, is the closed
  set of names that may be tested: testing a name outside it raises. `members`
  is either the materialized set or a membership predicate — the lazy form for
  sets whose membership is expensive to probe (only names a directive actually
  tests get probed)."""

  members: 'frozenset[str] | Callable[[str], bool]'
  universe: Optional[frozenset[str]] = None


Variables = Mapping[str, 'StringVariable | SetVariable | bool']

# what a condition operand resolves to: a variable's typed value or a bare-name
# string literal.
_Value = StringVariable | SetVariable | bool | str

_BUILTINS: Variables = {'true': True, 'false': False}


@dataclass(frozen=True)
class _Directive:
  keyword: str
  argument: str
  start: int
  end: int


def render(text: str, variables: Variables) -> str:
  """render the template against `variables` (plus the built-in `true`/`false`)."""
  overlap = set(variables) & set(_BUILTINS)
  if len(overlap) > 0:
    raise TemplateError(f'variables shadow built-ins: {", ".join(sorted(overlap))}')
  return _Renderer(text, {**_BUILTINS, **variables}).render()


class _Renderer:
  def __init__(self, text: str, variables: Variables):
    self._text = text
    self._variables = variables
    self._directives = [
      _Directive(match.group('keyword'), match.group('argument'), match.start(), match.end())
      for match in _DIRECTIVE_RE.finditer(text)
    ]
    self._position = 0

  def render(self) -> str:
    output, _ = self._render_until(stops=(), emit=True, cursor=0)
    return output

  def _next(self) -> Optional[_Directive]:
    if self._position >= len(self._directives):
      return None
    directive = self._directives[self._position]
    self._position += 1
    return directive

  def _render_until(
    self, stops: tuple[str, ...], emit: bool, cursor: int
  ) -> tuple[str, Optional[_Directive]]:
    """walk directives until one from `stops` at this nesting level (or the end
    of text), rendering (`emit`) or skipping the content in between. returns the
    rendered output and the stopping directive (None at end of text)."""
    parts: list[str] = []
    while True:
      directive = self._next()
      if directive is None:
        if emit:
          parts.append(self._text[cursor:])
        return ''.join(parts), None
      if emit:
        parts.append(self._text[cursor : directive.start])
      cursor = directive.end
      if directive.keyword in stops:
        return ''.join(parts), directive
      if directive.keyword == 'if':
        parts.append(self._render_if(directive, emit))
        cursor = self._directives[self._position - 1].end
      elif directive.keyword == 'assert':
        if emit:
          self._assert(directive)
      else:
        raise TemplateError(f'{{{{{directive.keyword}}}}} without a matching {{{{if}}}}')

  def _render_if(self, opening: _Directive, emit: bool) -> str:
    """render an if/elif/else/endif chain whose `{{if}}` was just consumed.
    conditions of every branch are evaluated for validation; at most one branch
    emits. leaves the position right after the chain's `{{endif}}`."""
    taken: Optional[str] = None
    branch_holds = self._evaluate(opening)
    seen_else = False
    while True:
      emit_branch = emit and branch_holds and taken is None
      output, stop = self._render_until(
        stops=('elif', 'else', 'endif'),
        emit=emit_branch,
        cursor=self._directives[self._position - 1].end,
      )
      if emit_branch:
        taken = output
      if stop is None:
        raise TemplateError(f'{{{{if{opening.argument}}}}} is missing its {{{{endif}}}}')
      if stop.keyword in ('else', 'endif') and len(stop.argument.strip()) > 0:
        raise TemplateError(f'{{{{{stop.keyword}}}}} takes no argument: {stop.argument.strip()!r}')
      if stop.keyword == 'endif':
        return taken if taken is not None else ''
      if seen_else:
        raise TemplateError(f'{{{{{stop.keyword}}}}} after {{{{else}}}}')
      if stop.keyword == 'elif':
        branch_holds = self._evaluate(stop)
      else:
        seen_else = True
        branch_holds = True

  def _assert(self, directive: _Directive) -> None:
    if not self._evaluate(directive):
      raise TemplateError(f'assertion failed: {directive.argument.strip()}')

  def _evaluate(self, directive: _Directive) -> bool:
    condition = directive.argument
    match = _CONDITION_RE.match(condition)
    if match is None:
      raise TemplateError(
        f'malformed condition {condition.strip()!r} in {{{{{directive.keyword}}}}}'
      )
    left = self._resolve(match.group('left'), condition)
    right = self._resolve(match.group('right'), condition)
    if match.group('operator') == '=':
      return self._equals(left, right, condition)
    return self._contains(left, right, condition)

  def _resolve(self, token: str, condition: str) -> _Value:
    if not token.startswith('#'):
      return token
    name = token[1:]
    if name not in self._variables:
      known = ', '.join(sorted(self._variables))
      raise TemplateError(f'unknown variable #{name} in {condition.strip()!r}; known: {known}')
    return self._variables[name]

  def _equals(self, left: _Value, right: _Value, condition: str) -> bool:
    if isinstance(left, SetVariable) or isinstance(right, SetVariable):
      raise TemplateError(f'sets cannot be compared with = in {condition.strip()!r}; use ∈')
    if isinstance(left, bool) or isinstance(right, bool):
      if not (isinstance(left, bool) and isinstance(right, bool)):
        raise TemplateError(f'boolean compared against a string in {condition.strip()!r}')
      return left == right
    left_string = self._string_value(left, other=right, condition=condition)
    right_string = self._string_value(right, other=left, condition=condition)
    return left_string == right_string

  @staticmethod
  def _string_value(operand: 'StringVariable | str', other: _Value, condition: str) -> str:
    # a literal compared against a domain-closed variable must belong to the
    # domain — a misspelled literal is a bug, not a false comparison.
    if isinstance(operand, str):
      if isinstance(other, StringVariable) and other.domain is not None:
        if operand not in other.domain:
          raise TemplateError(
            f'literal {operand!r} outside the domain of its comparand in {condition.strip()!r}; '
            f'domain: {", ".join(sorted(other.domain))}'
          )
      return operand
    return operand.value

  @staticmethod
  def _contains(left: _Value, right: _Value, condition: str) -> bool:
    if not isinstance(right, SetVariable):
      raise TemplateError(f'right side of ∈ is not a set in {condition.strip()!r}')
    if isinstance(left, str):
      element = left
    elif isinstance(left, StringVariable):
      element = left.value
    else:
      raise TemplateError(f'left side of ∈ is not a string in {condition.strip()!r}')
    if right.universe is not None and element not in right.universe:
      raise TemplateError(
        f'{element!r} outside the set universe in {condition.strip()!r}; '
        f'universe: {", ".join(sorted(right.universe))}'
      )
    if callable(right.members):
      return right.members(element)
    return element in right.members
