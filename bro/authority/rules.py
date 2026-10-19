"""Pure normalization and authorization transitions for declared mission types."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

from .authority import Atom, Authority, Cap, Entry, Pair, Permission, partition
from .patterns import Descriptor, PatternSet
from .values import Subsets, Value, ValueSet


class Refusal(ValueError):
  def __init__(self, rule: str, subject: object, reason: str):
    self.rule, self.subject, self.reason = rule, subject, reason
    from .syntax import render_atom, render_permission
    rendered = render_atom(subject) if isinstance(subject, Atom) else render_permission(subject) if isinstance(subject, Permission) else str(subject)
    super().__init__(f'{rule}: {rendered}: {reason}')


@dataclass(frozen=True)
class Option:
  kind: Literal['scalar', 'subset']
  domain: ValueSet
  open: bool = False
  overridable: bool = False

  def __post_init__(self) -> None:
    if self.kind not in ('scalar', 'subset') or (self.kind == 'subset') != isinstance(
      self.domain, Subsets
    ):
      raise ValueError('option kind and value-set family disagree')
    if self.domain.is_empty:
      raise ValueError('option domain must not be empty')

  @property
  def family(self) -> type[ValueSet]:
    return type(self.domain)


type Defaults = Callable[[Descriptor, object], Mapping[str, Value]]
type VariableDefaults = Callable[[Descriptor, Mapping[str, Value], object], Mapping[str, Value]]


def _no_defaults(values: Descriptor, reading: object) -> Mapping[str, Value]:
  return {}


def _no_variable_defaults(
  fixed: Descriptor, values: Mapping[str, Value], reading: object
) -> Mapping[str, Value]:
  return {}


def _valid(descriptor: Descriptor) -> None:
  pass


def _empty_base(fixed: Descriptor, reading: object) -> Authority:
  return Authority()


@dataclass(frozen=True)
class Declaration:
  options: Mapping[str, Option]
  defaults: Defaults = _no_defaults
  overridable_defaults: VariableDefaults = _no_variable_defaults
  validate: Callable[[Descriptor], None] = _valid
  base: Callable[[Descriptor, object], Authority] = _empty_base
  validate_joint: Callable[[Descriptor], None] = _valid

  def __post_init__(self) -> None:
    object.__setattr__(self, 'options', MappingProxyType(dict(self.options)))


@dataclass(frozen=True)
class Request:
  type: str
  values: Mapping[str, Value] = field(default_factory=dict)
  gift: Authority = Authority()
  cap: Authority = Cap()

  def __post_init__(self) -> None:
    object.__setattr__(self, 'values', MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class Admission:
  name: str
  check: Callable[[Descriptor], None]


@dataclass(frozen=True)
class Mission:
  request: Request
  descriptor: Descriptor
  reading: object
  base: Authority
  authority: Authority
  operator_values: frozenset[str] = frozenset()
  operator_gift: Authority = Authority()
  operator_cap: Authority = Cap()


@dataclass(frozen=True)
class Prepared:
  request: Request
  reading: object


@dataclass(frozen=True)
class Override:
  values: Mapping[str, Value] = field(default_factory=dict)
  clear: frozenset[str] = frozenset()
  gift: Authority | None = None
  cap: Authority | None = None

  def __post_init__(self) -> None:
    object.__setattr__(self, 'values', MappingProxyType(dict(self.values)))
    if self.clear & self.values.keys():
      raise ValueError('an override cannot set and clear the same option')


class Rules:
  def __init__(self, declarations: Mapping[str, Declaration]):
    self.declarations = MappingProxyType(dict(declarations))
    self.types = tuple(declarations)
    self.domains = MappingProxyType(
      {
        type: MappingProxyType(
          {name: option.domain for name, option in declaration.options.items()}
        )
        for type, declaration in declarations.items()
      }
    )

  def _declaration(self, type: str) -> Declaration:
    if type not in self.declarations:
      raise Refusal('declaration check', type, 'unknown type')
    return self.declarations[type]

  def _option(self, type: str, option: str) -> Option:
    declaration = self._declaration(type)
    if option not in declaration.options:
      raise Refusal('declaration check', f'{type}.{option}', 'unknown option')
    return declaration.options[option]

  def _values(self, descriptor: Descriptor) -> None:
    self._declaration(descriptor.type)
    for name, value in descriptor.values.items():
      if not self._option(descriptor.type, name).domain.contains(value):
        raise Refusal('domain check', f'{descriptor.type}.{name}', f'out-of-domain value {value!r}')

  def _permission(self, permission: Permission) -> None:
    self._declaration(permission.type)
    if permission.option is not None:
      option = self._option(permission.type, permission.option)
      assert permission.values is not None
      if (
        not option.domain.compatible(permission.values)
        or not permission.values.difference(option.domain).is_empty
      ):
        raise Refusal('value-set check', permission, 'value set outside option family or domain')

  def _patterns(self, patterns: PatternSet, use_type: str | None = None) -> None:
    for pattern in patterns.patterns:
      self._declaration(pattern.type)
      if use_type is not None and pattern.type != use_type:
        raise Refusal('pattern check', pattern.type, 'use pattern has the wrong type')
      for name in pattern.options:
        if self._option(pattern.type, name).overridable:
          raise Refusal(
            'authority-neutral check',
            f'{pattern.type}.{name}',
            'patterns cannot name overridable options',
          )
      for name, values in pattern.values.items():
        self._permission(Permission(pattern.type, name, values))

  def _pair(self, permission: Permission, pair: Pair) -> None:
    self._patterns(pair.use, permission.type)
    self._patterns(pair.give)

  def validate_authority(self, authority: Authority) -> None:
    for entry in authority.entries:
      self._permission(entry.permission)
      if isinstance(entry, Entry):
        self._pair(entry.permission, entry.pair)
    for operand in authority.operands:
      self.validate_authority(operand)

  def _resolve(self, authority: Authority, region: Permission, descriptor: Descriptor) -> Pair:
    pair = authority.evaluate_region(region, self._fixed(descriptor), self.types, self._pair)
    self._pair(region, pair)
    return pair

  def normalize(self, request: Request, reading: object) -> Descriptor:
    descriptor = Descriptor(request.type, request.values)
    self._values(descriptor)
    self.validate_authority(request.gift)
    self.validate_authority(request.cap)
    declaration = self._declaration(request.type)
    fixed = {
      name: value
      for name, value in request.values.items()
      if not declaration.options[name].overridable
    }
    variable = {
      name: value for name, value in request.values.items() if declaration.options[name].overridable
    }
    defaults = dict(declaration.defaults(Descriptor(request.type, fixed), reading))
    for name in defaults:
      if self._option(request.type, name).overridable:
        raise ValueError(f'fixed defaults returned overridable option {name}')
    fixed = {**defaults, **fixed}
    self._values(Descriptor(request.type, fixed))
    defaults = dict(
      declaration.overridable_defaults(
        Descriptor(request.type, fixed), MappingProxyType(variable), reading
      )
    )
    for name in defaults:
      if not self._option(request.type, name).overridable:
        raise ValueError(f'overridable defaults returned fixed option {name}')
    result = Descriptor(request.type, {**fixed, **defaults, **variable})
    self._values(result)
    try:
      declaration.validate(result)
    except ValueError as error:
      raise Refusal('validity check', result.type, str(error)) from error
    return result

  def use(
    self,
    authority: Authority,
    descriptor: Descriptor,
    *,
    options: frozenset[str] | None = None,
    start: bool = True,
    prepare: bool = False,
  ) -> None:
    self._values(descriptor)
    self.validate_authority(authority)
    atoms = [Atom(descriptor.type)] if start else []
    atoms.extend(
      Atom(descriptor.type, option, value)
      for option, value in descriptor.values.items()
      if options is None or option in options
    )
    for atom in atoms:
      pair = authority.evaluate(atom, self._fixed(descriptor), self.types, self._pair)
      self._pair(Permission(atom.type), pair)
      if not pair.use.matches(descriptor, prepare=prepare):
        raise Refusal('use rule', atom, 'no matching use pattern')

  def gift(
    self, starter: Authority, gift: Authority, descriptor: Descriptor, *, prepare: bool = False
  ) -> None:
    self.validate_authority(starter)
    self.validate_authority(gift)
    domains = [Permission(type) for type in self.types]
    domains.extend(
      Permission(type, name, option.domain)
      for type, declaration in self.declarations.items()
      for name, option in declaration.options.items()
    )
    for region in partition((*domains, *starter.permissions(), *gift.permissions())):
      offered = self._resolve(gift, region, descriptor)
      if offered.is_empty:
        continue
      held = self._resolve(starter, region, descriptor)
      if not prepare and not held.give.matches(descriptor):
        raise Refusal('gift rule', region, 'start is not a recipient')
      if not offered.give.within(held.give, self.domains):
        raise Refusal('gift rule', region, 'gift recipients exceed the giver recipients')

  def _admit(self, descriptor: Descriptor, checks: tuple[Admission, ...]) -> None:
    for admission in checks:
      try:
        admission.check(descriptor)
      except ValueError as error:
        raise Refusal(admission.name, descriptor.type, str(error)) from error

  def _fixed(self, descriptor: Descriptor) -> Descriptor:
    declaration = self._declaration(descriptor.type)
    return Descriptor(
      descriptor.type,
      {
        name: value
        for name, value in descriptor.values.items()
        if not declaration.options[name].overridable
      },
    )

  def _mission(
    self,
    request: Request,
    descriptor: Descriptor,
    reading: object,
    operator_values: frozenset[str] = frozenset(),
  ) -> Mission:
    declaration = self._declaration(request.type)
    base = declaration.base(self._fixed(descriptor), reading)
    self.validate_authority(base)
    return Mission(
      request, descriptor, reading, base, base.join(request.gift).meet(request.cap), operator_values
    )

  def start(
    self, request: Request, starter: Authority, reading: object, checks: tuple[Admission, ...] = ()
  ) -> Mission:
    descriptor = self.normalize(request, reading)
    self.use(starter, descriptor)
    self.gift(starter, request.gift, descriptor)
    self._admit(descriptor, checks)
    return self._mission(request, descriptor, reading)

  def prepare(self, request: Request, starter: Authority, reading: object) -> Prepared:
    descriptor = Descriptor(request.type, request.values)
    self._values(descriptor)
    self.validate_authority(request.cap)
    self.use(starter, descriptor, prepare=True)
    self.gift(starter, request.gift, descriptor, prepare=True)
    return Prepared(request, reading)

  def commit(
    self,
    prepared: Prepared,
    values: Mapping[str, Value],
    starter: Authority,
    operator: Authority,
    checks: tuple[Admission, ...] = (),
  ) -> Mission:
    request = replace(prepared.request, values={**prepared.request.values, **values})
    descriptor = self.normalize(request, prepared.reading)
    self._admit(
      descriptor,
      (Admission('joint validity check', self._declaration(request.type).validate_joint),),
    )
    self.use(starter, descriptor, options=frozenset(descriptor.values) - values.keys())
    self.use(operator, descriptor, options=frozenset(values), start=False)
    self.gift(starter, request.gift, descriptor)
    self._admit(descriptor, checks)
    return self._mission(request, descriptor, prepared.reading, frozenset(values))

  def override(
    self,
    mission: Mission,
    override: Override,
    operator: Authority,
    checks: tuple[Admission, ...] = (),
  ) -> Mission:
    for name in override.values.keys() | override.clear:
      if not self._option(mission.descriptor.type, name).overridable:
        raise Refusal('override check', name, 'option is not overridable')
    values = {
      name: value for name, value in mission.request.values.items() if name not in override.clear
    }
    request = replace(mission.request, values={**values, **override.values})
    descriptor = self.normalize(request, mission.reading)
    gift = mission.operator_gift if override.gift is None else override.gift
    cap = mission.operator_cap if override.cap is None else override.cap
    self.validate_authority(cap)
    changed = frozenset(
      name
      for name, value in descriptor.values.items()
      if mission.descriptor.values.get(name) != value
    )
    self.use(operator, descriptor, options=changed)
    self.gift(operator, gift, descriptor)
    self._admit(descriptor, checks)
    authority = mission.base.join(request.gift).meet(request.cap).join(gift).meet(cap)
    return replace(
      mission,
      request=request,
      descriptor=descriptor,
      authority=authority,
      operator_values=(mission.operator_values - override.clear) | override.values.keys(),
      operator_gift=gift,
      operator_cap=cap,
    )
