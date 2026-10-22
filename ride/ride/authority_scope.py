"""Bro option normalization, credential manifests, and base authorities."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import cast

from bro.authority.authority import Authority, Entry, Pair, Permission
from bro.authority.patterns import Descriptor, Pattern, PatternSet, Predicate
from bro.authority.rules import Declaration, Option, Request
from bro.authority.syntax import parse_entry, parse_permission
from bro.authority.values import (
  Finite,
  IntegerInterval,
  Integers,
  Scalars,
  StringGrammar,
  Strings,
  Subsets,
  Symbol,
  Value,
)
from bro.base.credentials import INSTANCE_CHARACTERS
from bro.bro import CredentialManifest
from bro.broker.environment import TALK_RIGHTS
from bro.llm import providers
from bro.llm.llm import EFFORT_LEVELS, LLMSpec
from bro.mcp import HOLDS
from bro.summon import DEFAULT_TALK, DEFAULT_TIMEOUT
from ride.authority_reading import Reading

DEFAULT = Symbol('default')

NAME_GRAMMAR = StringGrammar(
  frozenset('abcdefghijklmnopqrstuvwxyz'), frozenset('abcdefghijklmnopqrstuvwxyz0123456789-')
)


def finite(*members: Value) -> Finite:
  base = frozenset(members)
  return Finite(base, base)


def seconds(*sentinels: str) -> Scalars:
  return Scalars((Integers((IntegerInterval(1, None),)), finite(*sentinels)))


def _reading(value: object) -> Reading:
  if not isinstance(value, Reading):
    raise TypeError('bro declarations require a Reading')
  return value


def recipe(values: Mapping[str, Value], reading: Reading) -> LLMSpec | None:
  target = cast(str, values.get('target', ''))
  name = cast(str, values.get('harness', reading.summon_harness))
  installed = reading.installation
  if target not in installed.bros or name not in installed.harnesses:
    return None
  driver = installed.harnesses[name]
  selections = [
    providers.LLMSelection() if text == 'default' else providers.parse(text)
    for text in reading.recipes(target)
  ]
  selection = next((item for item in selections if driver.harness.runs(item)), None)
  if selection is None:
    raise ValueError(f'bro {target}: no configured recipe runs on harness {name}')
  requested = providers.LLMSelection(
    provider=cast(str | None, values.get('provider')),
    model=cast(str | None, values.get('model')) if values.get('model') != DEFAULT else None,
    effort=cast(str, values['effort']) if values.get('effort') in EFFORT_LEVELS else None,
    fast=values.get('fast') is True,
  )
  spec = driver.harness.resolve_recipe(driver.recipes[target], requested.over(selection))
  effective = spec.selection()
  if values.get('model') == DEFAULT:
    effective['model'] = None
  if 'effort' in values:
    effective['effort'] = None if values['effort'] == DEFAULT else cast(str, values['effort'])
  return spec.with_selection(effective)


def defaults(fixed: Descriptor, source: object) -> Mapping[str, Value]:
  reading = _reading(source)
  result: dict[str, Value] = {
    'placement': 'boxed',
    'harness': reading.summon_harness,
    'hold': 'unattended',
    'talk': DEFAULT_TALK,
    'timeout': DEFAULT_TIMEOUT,
  }
  values = {**result, **fixed.values}
  spec = recipe(values, reading)
  if spec is not None:
    data = spec.dump()
    result['provider'] = spec.TYPE
    selected = spec.selection()
    if 'model' in fixed.values and fixed.values['model'] != (
      selected['model'] if selected['model'] is not None else DEFAULT
    ):
      raise ValueError(f'bro.model must use its canonical model id {selected["model"]!r}')
    result.update({name: DEFAULT if value is None else value for name, value in selected.items()})
    if data.get('fast_mode') or data.get('service_tier') == 'priority':
      result['fast'] = True
  return result


def configured_kinds(target: str, harness: str, provider: str, reading: Reading) -> frozenset[str]:
  installed = reading.installation
  if target not in installed.bros or harness not in installed.harnesses:
    return frozenset()
  kinds: set[str] = {'trails'}
  bro = installed.bros.get(target)
  driver = installed.harnesses[harness]
  if bro is not None:
    kinds.update(bro.possible_credential_kinds(driver.harness, installed.credential_kinds))
  if driver.auth_kind is not None:
    kinds.add(driver.auth_kind)
  if driver.llm_key and provider:
    if provider not in installed.provider_needs:
      raise ValueError(f'provider {provider!r} is not installed')
    kinds.update(installed.provider_needs[provider])
  for policy in reading.layers(target):
    for entries, action in (
      (policy.layer.revoke, kinds.difference_update),
      (policy.layer.grant, kinds.update),
    ):
      action(
        entry.permission.type
        for entry in entries
        if entry.permission.option is None and entry.permission.type in installed.credential_kinds
      )
  unknown = kinds - installed.credential_kinds
  if unknown:
    raise ValueError(f'bro {target}: unknown credential kinds {sorted(unknown)}')
  return frozenset(kinds)


def credential_defaults(
  fixed: Descriptor, variable: Mapping[str, Value], source: object
) -> Mapping[str, Value]:
  reading = _reading(source)
  target = cast(str, fixed.values.get('target', ''))
  harness = cast(str, fixed.values['harness'])
  provider = cast(str, fixed.values.get('provider', ''))
  result: dict[str, Value] = {'add': frozenset(), 'drop': frozenset()}
  held = (
    configured_kinds(target, harness, provider, reading)
    | cast(frozenset[str], variable.get('add', frozenset()))
  ) - cast(frozenset[str], variable.get('drop', frozenset()))
  picks = reading.picks(target)
  result.update({f'cred.{kind}': picks.get(kind, '') for kind in held})
  return result


def validate(descriptor: Descriptor) -> None:
  if 'target' not in descriptor.values:
    raise ValueError('target must be set')
  dropped = cast(frozenset[str], descriptor.values.get('drop', frozenset()))
  for kind in sorted(dropped):
    if f'cred.{kind}' in descriptor.values:
      raise ValueError(f'bro.cred.{kind} cannot be both set and dropped through bro.drop')


def declaration(kinds: frozenset[str]) -> Declaration:
  names = Strings(cofinite=True, grammar=NAME_GRAMMAR)
  options = {
    name: Option('scalar', names, open=name != 'target')
    for name in ('target', 'harness', 'provider')
  }
  options.update(
    {
      'placement': Option('scalar', finite('boxed', 'unboxed', 'join')),
      'model': Option('scalar', Scalars((Strings(cofinite=True), finite(DEFAULT))), open=True),
      'effort': Option(
        'scalar', Scalars((finite(*providers.effort_values()), finite(DEFAULT))), open=True
      ),
      'fast': Option('scalar', finite(True), open=True),
      'hold': Option('scalar', finite(*HOLDS), open=True),
      'talk': Option('subset', Subsets(TALK_RIGHTS).universe(), open=True),
      'timeout': Option('scalar', seconds('none'), open=True),
      'add': Option('subset', Subsets(kinds).universe(), open=True, overridable=True),
      'drop': Option('subset', Subsets(kinds).universe(), open=True, overridable=True),
    }
  )
  options.update(
    {
      f'cred.{kind}': Option(
        'scalar',
        Strings(
          cofinite=True, grammar=StringGrammar(INSTANCE_CHARACTERS, INSTANCE_CHARACTERS, empty=True)
        ),
        overridable=True,
      )
      for kind in kinds
    }
  )
  return Declaration(options, defaults, credential_defaults, validate, base, validate_joint)


def configured_entries(reading: Reading) -> tuple[Entry, ...]:
  """One entry per configured (kind, instance), usable by the starts that configure it.

  Computed once per reading, so later starts under it do no per-target work.
  """
  if 'configured_entries' not in reading.derived:
    reading.derived['configured_entries'] = _configured_entries(reading)
  return reading.derived['configured_entries']


def _configured_entries(reading: Reading) -> tuple[Entry, ...]:
  installed = reading.installation
  targets = set(installed.bros)
  for project in reading.configuration.projects.values():
    targets.update(project.bros)

  @cache
  def held(target: str, harness: str, provider: str) -> frozenset[str]:
    return configured_kinds(target, harness, provider, reading)

  def configured(kind: str, instance: str) -> Predicate:
    def holds(descriptor: Descriptor, prepare: bool) -> bool:
      axes = [descriptor.values.get(name) for name in ('target', 'harness', 'provider')]
      if None in axes:
        return prepare
      target, harness, provider = cast(list[str], axes)
      return (
        reading.picks(target).get(kind, '') == instance
        and provider in installed.provider_needs
        and kind in held(target, harness, provider)
      )

    return Predicate(f'configured={kind}+{instance}', holds)

  instances = {
    kind: {reading.picks(target).get(kind, '') for target in targets}
    for kind in installed.credential_kinds
  }
  return tuple(
    Entry(
      Permission(
        'bro',
        f'cred.{kind}',
        installed.declarations['bro'].options[f'cred.{kind}'].domain.singleton(instance),
      ),
      Pair(PatternSet((Pattern('bro', predicates=frozenset({configured(kind, instance)})),))),
    )
    for kind in sorted(instances)
    for instance in sorted(instances[kind])
  )


def base(fixed: Descriptor, source: object) -> Authority:
  reading = _reading(source)
  installed = reading.installation
  types = tuple(installed.declarations)
  entries = [
    Entry(Permission(type, name, option.domain), Pair.top(type, types))
    for type, declaration in installed.declarations.items()
    for name, option in declaration.options.items()
    if option.open
  ]
  entries.append(Entry(Permission('bro'), Pair.top('bro', types)))
  entries.append(
    Entry(
      reading.syntax.permission(parse_permission('bro.placement=boxed')), Pair.top('bro', types)
    )
  )
  authority = Authority((*entries, *configured_entries(reading)))
  target = cast(str, fixed.values['target'])
  bro = installed.bros.get(target)
  if bro is not None:
    for permission in (*(f'@{name}' for name in bro._may_summon), *bro._may_launch):
      authority = authority.join(Authority((reading.syntax.entry(parse_entry(permission)),)))
  for policy in reading.layers(target):
    authority = reading.resolve(policy).apply(authority)
  return authority


def existence(
  descriptor: Descriptor,
  reading: Reading,
  present: frozenset[str],
  *,
  explicit: frozenset[str] = frozenset(),
  into_exists: bool = True,
  check_presence: bool = True,
  recording: bool = True,
) -> CredentialManifest:
  values = descriptor.values
  target, harness = cast(str, values['target']), cast(str, values['harness'])
  installed = reading.installation
  if target not in installed.bros:
    raise ValueError(f'target {target!r} is not installed')
  if harness not in installed.harnesses:
    raise ValueError(f'harness {harness!r} is not installed')
  if not into_exists:
    raise ValueError('into is not resolvable')
  driver = installed.harnesses[harness]
  held = frozenset(name.removeprefix('cred.') for name in values if name.startswith('cred.'))
  selected = installed.bros[target].credential_manifest(
    driver.harness, present & held, installed.credential_kinds
  )
  required, optional = set(selected.required), set(selected.optional) & held
  if recording:
    optional.update({'trails'} & held)
  if driver.auth_kind is not None:
    required.add(driver.auth_kind)
  if driver.llm_key:
    required.update(installed.provider_needs[cast(str, values['provider'])])
  configured = set()
  for policy in reading.layers(target):
    resolved = reading.resolve(policy)
    configured.difference_update(resolved.drop)
    configured.update(resolved.hold)
  required.update(configured & held)
  required.update(
    cast(frozenset[str], values.get('add', frozenset()))
    - cast(frozenset[str], values.get('drop', frozenset()))
  )
  required.update(explicit)
  optional.difference_update(required)
  required.update(
    kind for kind in optional if kind in reading.picks(target) or values.get(f'cred.{kind}') != ''
  )
  optional.difference_update(required)
  if required - held:
    raise ValueError(f'required kinds are not held: {sorted(required - held)}')
  if check_presence and required - present:
    raise ValueError(f'required credential instances are missing: {sorted(required - present)}')
  return CredentialManifest(
    frozenset(required), frozenset(optional & held & (present if check_presence else held))
  )


def option_values(fields: Mapping[str, object], source: object):
  from bro.base import credentials
  from bro.worker_types import OptionValues

  reading = _reading(source)
  options = set(reading.installation.declarations['bro'].options)
  content_names = {'prompt', 'into', 'share', 'step_id', 'index'}
  unknown = fields.keys() - options - content_names - {'llm', 'pass', 'manual'}
  if unknown:
    raise ValueError(f'unknown bro fields: {sorted(unknown)}')
  values = {name: cast(Value, value) for name, value in fields.items() if name in options}
  content = {name: value for name, value in fields.items() if name in content_names}
  if 'into' in content and (not isinstance(content['into'], str) or not content['into']):
    raise ValueError('content check: bro.into must be a non-empty git ref')
  for name in ('step_id', 'index'):
    if name in content:
      value = content[name]
      if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f'content check: bro.{name} must be a non-negative int')
  if 'index' in content and 'step_id' not in content:
    raise ValueError('content check: bro.index requires step_id')
  manual = fields.get('manual', False)
  if type(manual) is not bool:
    raise ValueError('manual must be a flag')
  if manual:
    refused = fields.keys() & {
      'placement',
      'party',
      'isolation',
      'harness',
      'hold',
      'timeout',
      'provider',
      'model',
      'effort',
      'fast',
      'llm',
      'share',
    }
    if refused:
      raise ValueError(f'joint prepare cannot set launch-owned fields: {sorted(refused)}')
  if fields.get('placement') == 'join' and (manual or 'into' in fields):
    raise ValueError('validity check: bro.placement=join refuses manual and into')
  if 'timeout' in fields and fields['timeout'] is None:
    values['timeout'] = 'none'
  for name in ('talk', 'add', 'drop'):
    if name in fields:
      members = fields[name]
      if not isinstance(members, (list, tuple, frozenset)) or any(
        type(item) is not str for item in members
      ):
        raise ValueError(f'{name} must be a list of strings')
      values[name] = frozenset(members)
  if 'talk' in values:
    values['talk'] = cast(frozenset[str], values['talk']) | {'worker.say'}
  for flag in ('fast',):
    if flag in values:
      if type(values[flag]) is not bool:
        raise ValueError(f'{flag} must be a flag')
      if values[flag] is False:
        del values[flag]
  if 'pass' in fields:
    passes = fields['pass']
    if not isinstance(passes, (list, tuple)):
      raise ValueError('pass must be a list of credential instances')
    picks = credentials.parse_picks(list(passes), where='bro', field='pass')
    for kind, instance in picks.items():
      name = f'cred.{kind}'
      if name in values:
        raise ValueError(f'{name} is set twice')
      values[name] = instance
  if 'llm' in fields:
    text = fields['llm']
    if not isinstance(text, str):
      raise ValueError('llm must be a recipe string')
    presets = {**reading.project.get('llm', {}), **reading.configuration.raw.get('llm', {})}
    selection = providers.parse(presets.get(text, text))
    for name in ('provider', 'model', 'effort'):
      value = getattr(selection, name)
      if value is not None:
        values.setdefault(name, value)
    if selection.fast and 'fast' not in fields:
      values['fast'] = True
  if 'model' in values and values['model'] != DEFAULT:
    spec = recipe(values, reading)
    if spec is not None:
      values['model'] = spec.dump()['model']
  return OptionValues(values, content)


def validate_joint(descriptor: Descriptor) -> None:
  if descriptor.values.get('placement') == 'join':
    raise ValueError('a joint start cannot join')


def checked_recipe(request: Request, descriptor: Descriptor, reading: Reading) -> LLMSpec:
  spec = recipe(request.values, reading)
  if spec is None:
    raise ValueError('cannot materialize a recipe for an uninstalled target or harness')
  if descriptor.values['provider'] != spec.TYPE:
    raise ValueError('descriptor provider differs from the requested recipe')
  selected = {
    name: None if descriptor.values[name] == DEFAULT else cast(str, descriptor.values[name])
    for name in spec.selection()
  }
  return spec.with_selection(selected)
