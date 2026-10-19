"""Permission strings, with installation-independent parsing and explicit resolution."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .authority import Authority, Cap, Entry, LazyEntry, Pair, Permission, partition
from .patterns import Pattern, PatternSet
from .rules import Declaration, Rules
from .values import (
  Finite,
  IntegerInterval,
  Integers,
  LatticeInterval,
  Scalars,
  Strings,
  Subsets,
  Symbol,
  Value,
  ValueSet,
)

_NAME = re.compile(r'[a-z][a-z0-9_-]*')
_WORD = re.compile(r'[A-Za-z0-9_:/.-]+')
_RANGE = re.compile(r'(-?\d+)?\.\.(-?\d+)?')
_TOKEN = re.compile(
  r'~[a-z][a-z0-9_-]*|\s+|"(?:[^"\\]|\\.)*"|\?=|<=|>=|[{}\[\](),|&!*=+@]|[A-Za-z0-9_:/.-]+'
)


def _name(text: str) -> str:
  if _NAME.fullmatch(text) is None:
    raise ValueError(f'invalid name {text!r}')
  return text


def _string(token: str) -> str:
  return json.loads(token) if token.startswith('"') else token


def _quote(value: str) -> str:
  if (
    value and value not in ('for', 'to') and _WORD.fullmatch(value) and not _RANGE.fullmatch(value)
  ):
    return value
  return json.dumps(value, ensure_ascii=False)


def _flag(values: ValueSet) -> bool:
  return isinstance(values, Finite) and len(values.base) == 1 and next(iter(values.base)) is True


def _union(left: ValueSet, right: ValueSet) -> ValueSet:
  return left.universe().difference(
    left.universe().difference(left).intersection(right.universe().difference(right))
  )


@dataclass(frozen=True)
class ValueExpression:
  operation: str
  items: tuple[str, ...] = ()
  operands: tuple[ValueExpression, ...] = ()

  def resolve(self, domain: ValueSet) -> ValueSet:
    match self.operation:
      case 'all':
        return domain
      case 'not':
        return domain.difference(self.operands[0].resolve(domain))
      case 'union' | 'intersection':
        left, right = (operand.resolve(domain) for operand in self.operands)
        return _union(left, right) if self.operation == 'union' else left.intersection(right)
      case 'subset' | 'atmost' | 'atleast':
        if not isinstance(domain, Subsets):
          raise ValueError('subset expression requires a subset option')
        members = frozenset(_string(item) for item in self.items)
        if not members <= domain.base:
          raise ValueError('subset members outside base')
        lower = frozenset() if self.operation == 'atmost' else members
        upper = domain.base if self.operation == 'atleast' else members
        result: ValueSet = Subsets(domain.base, (LatticeInterval(lower, upper),))
      case 'range':
        numbers = (
          domain.component(int)
          if isinstance(domain, Scalars) and int in domain.scalar_types
          else domain
        )
        if not isinstance(numbers, Integers):
          raise ValueError('integer range requires an integer option')
        lower, upper = self.items
        result = Integers(
          (IntegerInterval(int(lower) if lower else None, int(upper) if upper else None),)
        )
        if isinstance(domain, Scalars):
          result = domain.embed(result)
      case 'scalar' | 'finite':
        result = domain.empty()
        for item in self.items:
          value: Value = Symbol(item[1:]) if item.startswith('~') else _string(item)
          if (
            isinstance(domain, Integers)
            or isinstance(domain, Scalars)
            and int in domain.scalar_types
            and re.fullmatch(r'-?\d+', item)
            or isinstance(domain, Finite)
            and any(type(member) is int for member in domain.base)
          ):
            if not re.fullmatch(r'-?\d+', item):
              raise ValueError(f'expected an integer, got {item!r}')
            value = int(item)
          elif isinstance(domain, Finite) and any(type(member) is bool for member in domain.base):
            if item not in ('on', 'off'):
              raise ValueError(f'expected on or off, got {item!r}')
            value = item == 'on'
          if isinstance(domain, Scalars) and bool in domain.scalar_types and item in ('on', 'off'):
            value = item == 'on'
          if isinstance(domain, Subsets):
            raise ValueError('subset values require brackets')
          result = _union(result, domain.singleton(value))
      case _:
        raise ValueError(f'unknown value operation {self.operation!r}')
    if not result.difference(domain).is_empty:
      raise ValueError('value set outside option domain')
    return result


@dataclass(frozen=True)
class ParsedPermission:
  type: str
  option: str | None = None
  values: ValueExpression | None = None


@dataclass(frozen=True)
class Condition:
  option: str
  mode: str
  values: ValueExpression | None = None


@dataclass(frozen=True)
class ParsedPattern:
  type: str
  conditions: tuple[Condition, ...] = ()


@dataclass(frozen=True)
class ParsedEntry:
  permission: ParsedPermission
  use: tuple[ParsedPattern, ...] | None = None
  give: tuple[ParsedPattern, ...] | None = None

  @property
  def qualified(self) -> bool:
    return self.use is not None or self.give is not None


class _Parser:
  def __init__(self, text: str):
    self.tokens: list[str] = []
    position = 0
    for match in _TOKEN.finditer(text):
      if match.start() != position:
        raise ValueError(f'invalid syntax at {text[position:]!r}')
      position = match.end()
      token = match.group()
      if not token.isspace():
        if token.startswith('"'):
          json.loads(token)
        self.tokens.append(token)
    if position != len(text):
      raise ValueError(f'invalid syntax at {text[position:]!r}')
    self.position = 0

  @property
  def next(self) -> str | None:
    return self.tokens[self.position] if self.position < len(self.tokens) else None

  def take(self, expected: str | None = None) -> str:
    token = self.next
    if token is None or expected is not None and token != expected:
      raise ValueError(f'expected {expected or "token"}, got {token!r}')
    self.position += 1
    return token

  def finish(self) -> None:
    if self.next is not None:
      raise ValueError(f'unexpected token {self.next!r}')

  def scalar(self) -> str:
    token = self.take()
    if not token.startswith(('"', '~')) and _WORD.fullmatch(token) is None:
      raise ValueError(f'expected a value, got {token!r}')
    return token

  def items(self, closing: str) -> tuple[str, ...]:
    result = []
    if self.next != closing:
      result.append(self.scalar())
      while self.next == ',':
        self.take(',')
        result.append(self.scalar())
    self.take(closing)
    return tuple(result)

  def expression(self, precedence: int = 0) -> ValueExpression:
    if precedence < 2:
      left = self.expression(precedence + 1)
      operator = '|' if precedence == 0 else '&'
      while self.next == operator:
        self.take(operator)
        left = ValueExpression(
          'union' if operator == '|' else 'intersection',
          operands=(left, self.expression(precedence + 1)),
        )
      return left
    token = self.take()
    if token == '!':
      return ValueExpression('not', operands=(self.expression(2),))
    if token == '(':
      result = self.expression()
      self.take(')')
      return result
    if token == '*':
      return ValueExpression('all')
    if token == '{':
      return ValueExpression('finite', self.items('}'))
    if token in ('[', '<=', '>='):
      if token != '[':
        self.take('[')
      return ValueExpression(
        {'[': 'subset', '<=': 'atmost', '>=': 'atleast'}[token], self.items(']')
      )
    if match := _RANGE.fullmatch(token):
      return ValueExpression('range', tuple(part or '' for part in match.groups()))
    self.position -= 1
    return ValueExpression('scalar', (self.scalar(),))

  def option_values(self, option: str) -> tuple[str, ValueExpression]:
    if option == 'cred':
      kind = _name(self.take())
      self.take('+')
      option = f'cred.{kind}'
      following = self.tokens[self.position + 1 : self.position + 3]
      clause = (
        self.next in ('for', 'to')
        and bool(following)
        and (following[0] in ('@', '*') or len(following) == 2 and following[1] == '{')
      )
      if self.next is None or self.next in (',', '}') or clause:
        return option, ValueExpression('scalar', ('""',))
    return option, self.expression()

  def permission(self) -> ParsedPermission:
    if self.next == '@':
      self.take('@')
      target = self.take()
      if target != '*':
        _name(target)
      return ParsedPermission(
        'bro',
        'target',
        ValueExpression('all' if target == '*' else 'scalar', () if target == '*' else (target,)),
      )
    parts = self.take().split('.')
    type = _name(parts[0])
    if len(parts) == 1:
      return ParsedPermission(type)
    if len(parts) != 2:
      raise ValueError('expected type.option')
    option = _name(parts[1])
    if self.next == '=':
      self.take('=')
      option, values = self.option_values(option)
    else:
      values = None
    return ParsedPermission(type, option, values)

  def pattern(self) -> ParsedPattern:
    if self.next == '*':
      self.take('*')
      return ParsedPattern('*')
    if self.next == '@':
      permission = self.permission()
      return (
        ParsedPattern('bro')
        if permission.values == ValueExpression('all')
        else ParsedPattern('bro', (Condition('target', '=', permission.values),))
      )
    type = _name(self.take())
    self.take('{')
    conditions = []
    while self.next != '}':
      unset = self.next == '!'
      if unset:
        self.take('!')
      option = self.take()
      for segment in option.split('.'):
        _name(segment)
      mode = 'unset' if unset else 'set'
      values = None
      if not unset and self.next in ('=', '?='):
        mode = self.take()
        option, values = self.option_values(option)
      if any(condition.option == option for condition in conditions):
        raise ValueError(f'duplicate condition {option!r}')
      conditions.append(Condition(option, mode, values))
      if self.next != ',':
        break
      self.take(',')
      if self.next == '}':
        raise ValueError('expected a condition after comma')
    self.take('}')
    return ParsedPattern(type, tuple(conditions))

  def patterns(self) -> tuple[ParsedPattern, ...]:
    result = []
    while self.next is not None:
      if self.next in ('for', 'to') and self.tokens[self.position + 1 : self.position + 2] != ['{']:
        break
      result.append(self.pattern())
    if not result:
      raise ValueError('expected at least one pattern')
    return tuple(result)


def parse_values(text: str, domain: ValueSet) -> ValueSet:
  parser = _Parser(text)
  expression = parser.expression()
  parser.finish()
  return expression.resolve(domain)


def parse_permission(text: str) -> ParsedPermission:
  parser = _Parser(text)
  result = parser.permission()
  parser.finish()
  return result


def parse_pattern(text: str) -> ParsedPattern:
  parser = _Parser(text)
  result = parser.pattern()
  parser.finish()
  return result


def parse_entry(text: str) -> ParsedEntry:
  parser = _Parser(text)
  permission = parser.permission()
  use = give = None
  if parser.next == 'for':
    parser.take('for')
    use = parser.patterns()
  if parser.next == 'to':
    parser.take('to')
    give = parser.patterns()
  parser.finish()
  return ParsedEntry(permission, use, give)


@dataclass(frozen=True)
class Layer:
  grant: tuple[ParsedEntry, ...] = ()
  revoke: tuple[ParsedEntry, ...] = ()
  limit: tuple[ParsedEntry, ...] = ()


@dataclass(frozen=True)
class ResolvedLayer:
  gift: Authority = Authority()
  cap: Cap = field(default_factory=Cap)
  hold: frozenset[str] = frozenset()
  drop: frozenset[str] = frozenset()

  def apply(self, authority: Authority) -> Authority:
    return authority.meet(self.cap).join(self.gift)


def parse_layer(
  *,
  grant: Iterable[str] = (),
  revoke: Iterable[str] = (),
  limit: Iterable[str] = (),
  operator: bool = False,
) -> Layer:
  result = Layer(
    *(tuple(parse_entry(text) for text in entries) for entries in (grant, revoke, limit))
  )
  if operator and result.grant:
    raise ValueError('operator takes revoke and limit only')
  if any(entry.qualified for entry in result.revoke):
    raise ValueError('revoke takes plain permissions only')
  if any(not entry.qualified for entry in result.limit):
    raise ValueError('limit takes qualified entries only')
  return result


class Syntax:
  """Resolve only the layers a launch applies, against one installation's registries."""

  def __init__(self, declarations: Mapping[str, Declaration], credential_kinds: Iterable[str] = ()):
    self.declarations = MappingProxyType(dict(declarations))
    self.credential_kinds = frozenset(credential_kinds)
    overlap = self.declarations.keys() & self.credential_kinds
    if overlap:
      raise ValueError(f'names claimed as both credential kind and mission type: {sorted(overlap)}')
    for name in (*self.declarations, *self.credential_kinds):
      _name(name)
    self.rules = Rules(self.declarations)

  def _domain(self, type: str, option: str) -> ValueSet:
    if type not in self.declarations:
      raise ValueError(f'unknown mission type {type!r}')
    if option.startswith('cred.') and option.removeprefix('cred.') not in self.credential_kinds:
      raise ValueError(f'unknown credential kind {option.removeprefix("cred.")!r}')
    if option not in self.declarations[type].options:
      raise ValueError(f'unknown option {type}.{option}')
    return self.declarations[type].options[option].domain

  def permission(self, parsed: ParsedPermission) -> Permission:
    if parsed.type not in self.declarations:
      raise ValueError(f'unknown mission type {parsed.type!r}')
    if parsed.option is None:
      return Permission(parsed.type)
    domain = self._domain(parsed.type, parsed.option)
    if parsed.values is None:
      if not _flag(domain):
        raise ValueError(f'{parsed.type}.{parsed.option} is not a flag')
      values = domain
    else:
      values = parsed.values.resolve(domain)
    return Permission(parsed.type, parsed.option, values)

  def pattern(self, parsed: ParsedPattern, *, recipient: bool = False) -> Pattern:
    if parsed.type not in self.declarations:
      raise ValueError(f'unknown mission type {parsed.type!r}')
    values = {}
    required = set()
    unset = set()
    for condition in parsed.conditions:
      domain = self._domain(parsed.type, condition.option)
      if condition.values is not None:
        values[condition.option] = condition.values.resolve(domain)
      if condition.mode in ('=', 'set'):
        required.add(condition.option)
      if condition.mode == 'unset':
        unset.add(condition.option)
    if (
      recipient
      and parsed.type == 'bro'
      and not any(condition.option == 'placement' for condition in parsed.conditions)
    ):
      domain = self._domain('bro', 'placement')
      values['placement'] = domain.difference(domain.singleton('join'))
    return Pattern(parsed.type, values, frozenset(required), frozenset(unset))

  def _patterns(
    self, parsed: tuple[ParsedPattern, ...] | None, type: str, *, recipient: bool = False
  ) -> PatternSet:
    result = PatternSet()
    for pattern in parsed or ():
      if pattern.type == '*':
        result = result.join(PatternSet.all(*(self.declarations if recipient else (type,))))
      else:
        result = result.join(PatternSet((self.pattern(pattern, recipient=recipient),)))
    return result

  def entry(self, parsed: ParsedEntry) -> Entry:
    permission = self.permission(parsed.permission)
    pair = (
      Pair(
        self._patterns(parsed.use, permission.type),
        self._patterns(parsed.give, permission.type, recipient=True),
      )
      if parsed.qualified
      else Pair.top(permission.type, self.declarations)
    )
    result = Entry(permission, pair)
    self.rules.validate_authority(Authority((result,)))
    return result

  def forward(self, parsed: ParsedEntry, giver: Authority) -> Authority:
    entry = self.entry(parsed)
    if parsed.qualified:
      return Authority((entry,))
    self.rules.validate_authority(giver)

    def forwarded(region: Permission) -> LazyEntry:
      return LazyEntry(
        region,
        lambda descriptor: Pair(
          PatternSet.all(region.type),
          giver.evaluate_region(region, descriptor, self.declarations).give,
        ),
      )

    return Authority(
      tuple(
        forwarded(region)
        for region in partition((entry.permission, *giver.permissions()))
        if region.intersection(entry.permission) is not None
      )
    )

  def layer(self, parsed: Layer) -> ResolvedLayer:
    hold: set[str] = set()
    drop: set[str] = set()
    grants = []
    caps = []
    permissions = []
    revoked = []
    for entries, action in (
      (parsed.grant, 'grant'),
      (parsed.revoke, 'revoke'),
      (parsed.limit, 'limit'),
    ):
      for entry in entries:
        permission = entry.permission
        if permission.option is None and permission.type in self.credential_kinds:
          if entry.qualified or action == 'limit':
            raise ValueError('credential kinds take plain grant or revoke only')
          (hold if action == 'grant' else drop).add(permission.type)
          continue
        resolved = self.entry(entry)
        if action == 'grant':
          grants.append(resolved)
          permissions.append(resolved.permission)
        elif action == 'revoke':
          caps.append(Entry(resolved.permission, Pair()))
          revoked.append(resolved.permission)
        else:
          caps.append(resolved)
    if hold & drop or any(
      left.intersection(right) is not None for left in permissions for right in revoked
    ):
      raise ValueError('cannot grant and revoke the same permission or credential kind')
    return ResolvedLayer(
      Authority(tuple(grants)), Cap(tuple(caps)), frozenset(hold), frozenset(drop)
    )


def _scalar(value: Value) -> str:
  if isinstance(value, Symbol):
    return '~' + value.name
  if type(value) is bool:
    return 'on' if value else 'off'
  if type(value) is int:
    return str(value)
  if isinstance(value, str):
    return _quote(value)
  raise ValueError('expected a scalar')


def _finite(members: Iterable[Value]) -> str:
  items = sorted(_scalar(value) for value in members)
  return items[0] if len(items) == 1 else '{' + ','.join(items) + '}'


def _subset(members: frozenset[str]) -> str:
  return '[' + ','.join(_quote(value) for value in sorted(members)) + ']'


def _prime_intervals(values: Subsets) -> tuple[LatticeInterval, ...]:
  intervals = set(values.intervals)
  while True:
    expanded = set(intervals)
    for left in intervals:
      for right in intervals:
        conflicts = (left.lower - right.upper) | (right.lower - left.upper)
        if len(conflicts) == 1:
          lower = (left.lower | right.lower) - conflicts
          upper = (left.upper & right.upper) | conflicts
          expanded.add(LatticeInterval(lower, upper))
    expanded = {
      item
      for item in expanded
      if not any(
        other != item and other.lower <= item.lower and item.upper <= other.upper
        for other in expanded
      )
    }
    if expanded == intervals:
      return tuple(sorted(intervals, key=lambda item: (sorted(item.lower), sorted(item.upper))))
    intervals = expanded


def _sum_literal(value: Value, family: Scalars) -> str:
  if isinstance(value, str):
    if (
      int in family.scalar_types
      and re.fullmatch(r'-?\d+', value)
      or bool in family.scalar_types
      and value in ('on', 'off')
    ):
      return json.dumps(value, ensure_ascii=False)
  return _scalar(value)


def _sum_finite(members: Iterable[Value], family: Scalars) -> str:
  items = sorted(_sum_literal(member, family) for member in members)
  return items[0] if len(items) == 1 else '{' + ','.join(items) + '}'


def _render_component(component: ValueSet, family: Scalars) -> str:
  if isinstance(component, Integers):
    return '..' if component == component.universe() else render_values(component)
  if isinstance(component, Finite):
    return _sum_finite(component.members, family)
  assert isinstance(component, Strings)
  if not component.cofinite:
    return _sum_finite(component.members, family)
  excluded = [
    _render_component(other.universe(), family)
    for other in family.components
    if other is not component
  ]
  if component.members:
    excluded.append(_sum_finite(component.members, family))
  text = '|'.join(excluded)
  return '!(' + text + ')' if '|' in text or '&' in text else '!' + text


def render_values(values: ValueSet) -> str:
  if values.is_empty:
    return '{}'
  if values == values.universe():
    return '*'
  if isinstance(values, Strings):
    return ('!' if values.cofinite else '') + _finite(values.members)
  if isinstance(values, Finite):
    return _finite(values.members)
  if isinstance(values, Integers):
    return '|'.join(
      str(interval.lower)
      if interval.lower == interval.upper
      else f'{interval.lower if interval.lower is not None else ""}..{interval.upper if interval.upper is not None else ""}'
      for interval in values.intervals
    )
  if isinstance(values, Scalars):
    return '|'.join(
      _render_component(component, values)
      for component in values.components
      if not component.is_empty
    )
  if isinstance(values, Subsets):
    terms = []
    for interval in _prime_intervals(values):
      if interval.lower == interval.upper:
        terms.append(_subset(interval.lower))
      else:
        bounds = []
        if interval.lower:
          bounds.append('>=' + _subset(interval.lower))
        if interval.upper != values.base:
          bounds.append('<=' + _subset(interval.upper))
        terms.append('&'.join(bounds) or '*')
    return '|'.join(terms)
  raise TypeError(f'unknown value-set family {type(values).__name__}')


def render_atom(atom) -> str:
  if atom.option is None:
    return atom.type
  if atom.type == 'bro' and atom.option == 'target':
    return '@' + str(atom.value)
  if atom.option.startswith('cred.'):
    return (
      atom.type
      + '.cred='
      + atom.option.removeprefix('cred.')
      + '+'
      + ('' if atom.value == '' else _scalar(atom.value))
    )
  if atom.value is True:
    return atom.type + '.' + atom.option
  return (
    atom.type
    + '.'
    + atom.option
    + '='
    + (_subset(atom.value) if isinstance(atom.value, frozenset) else _scalar(atom.value))
  )


def render_permission(permission: Permission) -> str:
  if permission.option is None:
    return permission.type
  assert permission.values is not None
  values = render_values(permission.values)
  if (
    permission.type == 'bro'
    and permission.option == 'target'
    and (
      values == '*'
      or isinstance(permission.values, Strings)
      and not permission.values.cofinite
      and len(permission.values.members) == 1
      and _NAME.fullmatch(next(iter(permission.values.members)))
    )
  ):
    if values == '*':
      return '@*'
    assert isinstance(permission.values, Strings)
    return '@' + next(iter(permission.values.members))
  if permission.option.startswith('cred.'):
    kind = permission.option.removeprefix('cred.')
    if (
      isinstance(permission.values, Strings)
      and not permission.values.cofinite
      and permission.values.members == {''}
    ):
      values = ''
    return f'{permission.type}.cred={kind}+{values}'
  if _flag(permission.values) and not permission.values.is_empty:
    return f'{permission.type}.{permission.option}'
  return f'{permission.type}.{permission.option}={values}'


def render_pattern(pattern: Pattern, *, recipient: bool = False) -> str:
  if (
    not recipient
    and pattern.type == 'bro'
    and pattern.options == {'target'}
    and 'target' in pattern.values
    and pattern.required == {'target'}
    and not pattern.unset
  ):
    shorthand = render_permission(Permission('bro', 'target', pattern.values['target']))
    if shorthand.startswith('@') and shorthand != '@*':
      return shorthand
  conditions = []
  for option in sorted(pattern.options):
    if option in pattern.unset:
      if option in pattern.required:
        name = 'cred' if option.startswith('cred.') else option
        prefix = option.removeprefix('cred.') + '+' if option.startswith('cred.') else ''
        conditions.append(name + '=' + prefix + '{}')
      else:
        conditions.append('!' + option)
    elif option in pattern.values:
      values = render_values(pattern.values[option])
      name = option
      if option.startswith('cred.'):
        name = 'cred'
        values = option.removeprefix('cred.') + '+' + values
      conditions.append(name + ('=' if option in pattern.required else '?=') + values)
    else:
      conditions.append(option)
  if recipient and pattern.type == 'bro' and 'placement' not in pattern.options:
    conditions.append('placement?=*')
  return pattern.type + '{' + ','.join(conditions) + '}'


def _render_patterns(patterns: PatternSet, *, recipient: bool = False) -> str:
  return ' '.join(
    sorted(
      {
        render_pattern(pattern, recipient=recipient)
        for pattern in patterns.patterns
        if not pattern.is_empty
      }
    )
  )


def render_entry(entry: Entry, types: Iterable[str]) -> str:
  permission = render_permission(entry.permission)
  top = Pair.top(entry.permission.type, types)
  if entry.pair == top:
    return permission
  clauses = []
  if not entry.pair.use.is_empty:
    clauses.append(
      'for ' + ('*' if entry.pair.use == top.use else _render_patterns(entry.pair.use))
    )
  if not entry.pair.give.is_empty:
    clauses.append(
      'to '
      + ('*' if entry.pair.give == top.give else _render_patterns(entry.pair.give, recipient=True))
    )
  if not clauses:
    raise ValueError('empty pairs are rendered as revoke permissions')
  return permission + ' ' + ' '.join(clauses)
