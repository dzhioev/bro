"""A root's frozen sources and installation for use/give declarations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from bro.authority.authority import Authority
from bro.authority.rules import Declaration, Rules
from bro.authority.syntax import Layer, Syntax, parse_layer
from bro.base import credentials, host_config, log
from bro.bro import BaseBro
from bro.harness import Harness
from bro.llm import providers
from bro.workspace.installation import installation_home
from bro.workspace.project import project_config_from_text
from bro.workspace.settings import HomeSettings, home_settings

if TYPE_CHECKING:
  from ride.repository import Repository


def _freeze(value: Any) -> Any:
  if isinstance(value, dict):
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})
  if isinstance(value, list):
    return tuple(_freeze(item) for item in value)
  return value


def _object(value: object, where: str, allowed: set[str] | None = None) -> dict[str, Any]:
  if not isinstance(value, dict):
    raise ValueError(f'{where} must be an object')
  if allowed is not None and value.keys() - allowed:
    raise ValueError(f'{where}: unknown keys {sorted(value.keys() - allowed)}')
  return value


@dataclass(frozen=True)
class Policy:
  config: host_config.ConfigLayer = host_config.ConfigLayer()
  source: str = ''
  foreign: bool = False
  home_settings: bool = False

  layer: Layer = field(init=False)
  picks: Mapping[str, str] = field(init=False)

  def __post_init__(self) -> None:
    object.__setattr__(
      self,
      'layer',
      parse_layer(grant=self.config.grant, revoke=self.config.revoke, limit=self.config.limit),
    )
    object.__setattr__(self, 'picks', MappingProxyType(self.config.selection))


@dataclass(frozen=True)
class HomePolicy:
  policy: Policy
  bros: Mapping[str, Policy]

  def __post_init__(self) -> None:
    object.__setattr__(self, 'bros', MappingProxyType(dict(self.bros)))


@dataclass(frozen=True)
class Configuration:
  defaults: Policy
  homes: Mapping[str, HomePolicy]
  operator: Layer
  user: tuple[Policy, ...] = ()

  def __post_init__(self) -> None:
    object.__setattr__(self, 'homes', MappingProxyType(dict(self.homes)))

  @classmethod
  def parse(
    cls, value: object, *, home: str | None = None, bro_homes: Mapping[str, str] | None = None
  ) -> Configuration:
    home = installation_home() if home is None else home
    if bro_homes is None:
      from bro import registry

      bro_homes = {bro: registry.home(bro) or home for bro in registry.known_names()}
    parsed = host_config.parse(value, home=home, bro_homes=bro_homes)
    homes = {
      name: HomePolicy(
        Policy(entry.layer, f'homes.{name}'),
        {
          bro: Policy(config, f'homes.{name}.bros.{bro}', foreign=name != home)
          for bro, config in entry.bros.items()
        },
      )
      for name, entry in parsed.homes.items()
    }
    user = [
      Policy(
        host_config.ConfigLayer(
          creds=tuple(f'{kind}+{instance}' for kind, instance in parsed.user.credentials.items())
        ),
        'user',
      )
    ]
    user.extend(
      Policy(
        host_config.ConfigLayer(
          creds=tuple(f'{kind}+{instance}' for kind, instance in picks.items())
        ),
        f'user.tools.{command}',
      )
      for command, picks in parsed.user.tools.items()
    )
    return cls(Policy(parsed.defaults, 'defaults'), homes, parsed.operator, tuple(user))


@dataclass(frozen=True)
class HarnessReading:
  harness: Harness
  auth_kind: str | None = None
  llm_key: bool = False


@dataclass(frozen=True)
class Installation:
  bros: Mapping[str, BaseBro]
  harnesses: Mapping[str, HarnessReading]
  credential_kinds: frozenset[str]
  provider_needs: Mapping[str, frozenset[str]]
  declarations: Mapping[str, Declaration]
  bro_homes: Mapping[str, str] = field(default_factory=dict)
  unavailable_harnesses: Mapping[str, str] = field(default_factory=dict)
  unavailable_bros: Mapping[str, str] = field(default_factory=dict)
  unavailable_providers: Mapping[str, str] = field(default_factory=dict)

  def __post_init__(self) -> None:
    for name in (
      'bros',
      'bro_homes',
      'harnesses',
      'provider_needs',
      'declarations',
      'unavailable_harnesses',
      'unavailable_bros',
      'unavailable_providers',
    ):
      object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

  @classmethod
  def take(cls) -> Installation:
    from bro import registry
    from bro.worker_types import installed_types
    from ride.harness import get_harness, installed_harness_names

    kinds = frozenset(credentials.default_registry())
    bros: dict[str, BaseBro] = {}
    unavailable_bros: dict[str, str] = {}
    for name in registry.known_names():
      try:
        bros[name] = registry.get_class(name)(credential_presence=kinds, credential_kinds=kinds)
      except Exception as error:
        unavailable_bros[name] = str(error)
        log.warning('bro %r is unavailable: %s', name, error)
    harnesses = {}
    unavailable_harnesses = {}
    for name in installed_harness_names():
      try:
        driver = get_harness(name)
        scope = driver.scope_recipe()
      except Exception as error:
        unavailable_harnesses[name] = str(error)
        log.warning('harness %r is unavailable: %s', name, error)
        continue
      harnesses[name] = HarnessReading(scope.harness, scope.auth_secret, scope.llm_key)
    provider_needs = {}
    unavailable_providers = {}
    for name in providers.known_names():
      try:
        provider_needs[name] = frozenset(providers.declaration(name).Recipe.needed_secrets())
      except Exception as error:
        unavailable_providers[name] = str(error)
        log.warning('provider %r is unavailable: %s', name, error)
    return cls(
      bros,
      harnesses,
      kinds,
      provider_needs,
      {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
      {name: registry.home(name) or installation_home() for name in registry.known_names()},
      unavailable_harnesses,
      unavailable_bros,
      unavailable_providers,
    )

  def check_harness(self, name: str) -> None:
    if name in self.unavailable_harnesses:
      raise ValueError(f'harness {name!r} is unavailable: {self.unavailable_harnesses[name]}')

  def check_bro(self, name: str) -> None:
    if name in self.unavailable_bros:
      raise ValueError(f'bro {name!r} is unavailable: {self.unavailable_bros[name]}')

  def check_provider(self, name: str) -> None:
    if name in self.unavailable_providers:
      raise ValueError(f'provider {name!r} is unavailable: {self.unavailable_providers[name]}')
    if name not in self.provider_needs:
      raise ValueError(
        f'unknown provider {name!r}; installed providers: {", ".join(sorted(self.provider_needs)) or "(none)"}; '
        'install the plug-in that provides it'
      )


@dataclass(frozen=True)
class Reading:
  configuration: Configuration
  store_defaults: Mapping[str, str]
  installation: Installation
  attachment: host_config.Attachment | None = None
  project: Mapping[str, Any] = field(default_factory=dict)
  home_policy: Policy = Policy()
  settings: HomeSettings = HomeSettings()
  home: str = field(default_factory=installation_home)
  derived: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

  def __post_init__(self) -> None:
    object.__setattr__(
      self,
      'store_defaults',
      MappingProxyType(
        credentials.registered_picks(
          self.store_defaults, self.installation.credential_kinds, where='store defaults'
        )
      ),
    )
    object.__setattr__(self, 'project', _freeze(dict(self.project)))
    if self.home_policy == Policy() and self.settings != HomeSettings():
      object.__setattr__(
        self, 'home_policy', Policy(self.settings, f'{self.home} home settings', home_settings=True)
      )
    self.validate()

  @classmethod
  def take(
    cls,
    repository: Repository | None = None,
    *,
    store: Path | None = None,
    host: Path | None = None,
    installation: Installation | None = None,
  ) -> Reading:
    from ride.repository import attachment_identities

    home = installation_home()
    installed = installation or Installation.take()
    configuration = Configuration.parse(
      json.loads((host or Path(host_config.HOST_CONFIG_FILE)).read_text())
      if (host or Path(host_config.HOST_CONFIG_FILE)).is_file()
      else {},
      home=home,
      bro_homes=installed.bro_homes,
    )
    directory = store or Path(credentials.STORE_DIR)
    index = Path(directory) / credentials.STORE_FILE
    data = _object(json.loads(index.read_text()), str(index)) if index.is_file() else {}
    defaults = credentials.parse_picks(data.get('defaults', []), where=str(index), field='defaults')
    home = installation_home()
    settings = home_settings(home)
    project = {}
    attachment = None
    if repository is not None:
      attachment = attachment_identities(repository.identity)
      content = repository.read_file('pyproject.toml')
      if content is not None:
        project = project_config_from_text(content.decode(), repository.identity).sections
    policy = Policy(settings, source=f'{home} home settings', home_settings=True)
    result = cls(configuration, defaults, installed, attachment, project, policy, settings, home)
    return result

  @property
  def syntax(self) -> Syntax:
    return Syntax(self.installation.declarations, self.installation.credential_kinds)

  @property
  def rules(self) -> Rules:
    return Rules(self.installation.declarations)

  def general_layers(self) -> tuple[Policy, ...]:
    home = self.configuration.homes.get(self.home)
    return (self.home_policy, self.configuration.defaults, *((home.policy,) if home else ()))

  @property
  def default_bro(self) -> str:
    for policy in reversed(self.general_layers()):
      if policy.config.default_bro is not None:
        return policy.config.default_bro
    raise ValueError(
      'no default bro: pass a bro or set default-bro in home settings or homes.' + self.home
    )

  def layers(self, target: str) -> tuple[Policy, ...]:
    result = list(self.general_layers())
    foreign = self.installation.bro_homes.get(target, self.home)
    for home in (foreign, self.home) if foreign != self.home else (self.home,):
      section = self.configuration.homes.get(home)
      if section is not None and target in section.bros:
        result.append(section.bros[target])
    return tuple(result)

  def winning(self, target: str, key: str) -> tuple[object, Policy] | None:
    for policy in reversed(self.layers(target)):
      value = getattr(policy.config, key)
      if value is not None:
        return value, policy
    return None

  @property
  def harness(self) -> str | None:
    winner = self.winning('', 'harness')
    return cast(str, winner[0]) if winner else None

  @property
  def summon_harness(self) -> str | None:
    winner = self.winning('', 'summon_harness')
    return cast(str, winner[0]) if winner else None

  def resolve_harness(
    self, selected: str | None = None, *, target: str = '', summon: bool = False
  ) -> str:
    if selected is not None:
      return selected
    key = 'summon_harness' if summon else 'harness'
    winner = self.winning(target, key)
    if winner is None:
      raise ValueError(
        f'no harness for {target}: pass --harness, or set "{key.replace("_", "-")}" in a layer of ~/.bro.json or in the home settings'
      )
    name = cast(str, winner[0])
    try:
      self.installation.check_harness(name)
      if name not in self.installation.harnesses:
        raise ValueError(f'harness {name!r} is not installed')
    except ValueError as error:
      raise self.failure(winner[1], key.replace('_', '-'), error, target) from error
    return name

  @property
  def summon_depth(self) -> int:
    from bro.base.configs import DEFAULT_SUMMON_DEPTH

    return next(
      (
        policy.config.summon_depth
        for policy in reversed(self.general_layers())
        if policy.config.summon_depth is not None
      ),
      DEFAULT_SUMMON_DEPTH,
    )

  def configured_picks(self, target: str) -> dict[str, str]:
    result = dict(self.store_defaults)
    for policy in self.layers(target):
      result.update(policy.picks)
    return result

  def picks(self, target: str) -> dict[str, str]:
    result = dict(self.store_defaults)
    sources = {}
    for policy in self.layers(target):
      result.update(policy.picks)
      sources.update(dict.fromkeys(policy.picks, policy))
    for kind, policy in sources.items():
      if kind not in self.installation.credential_kinds:
        raise self.failure(policy, 'creds', ValueError(f'unknown credential kind {kind!r}'), target)
    return result

  def failure(self, policy: Policy, key: str, error: Exception, target: str = '') -> ValueError:
    message = f'~/.bro.json: {policy.source}.{key}: {error}'
    if policy.foreign:
      message += f'; install the plug-in that provides it, or override the value in homes.{self.home}.bros.{target}'
    return ValueError(message)

  def resolve(self, policy: Policy, target: str = ''):
    try:
      if policy.home_settings:
        for entry in (*policy.layer.grant, *policy.layer.revoke, *policy.layer.limit):
          options = [entry.permission.option]
          options.extend(
            condition.option
            for pattern in (*(entry.use or ()), *(entry.give or ()))
            for condition in pattern.conditions
          )
          if any(option is not None and option.startswith('cred.') for option in options):
            raise ValueError('home settings cannot name credential instances')

      return self.bind_policy(policy, policy.layer, target)
    except ValueError as error:
      raise self.failure(policy, 'authority', error, target) from error

  def bind_policy(self, policy: Policy, layer: Layer, target: str = ''):
    for key in ('grant', 'revoke', 'limit'):
      try:
        self.bind_layer(Layer(**{key: getattr(layer, key)}))
      except ValueError as error:
        raise self.failure(policy, key, error, target) from error
    return self.bind_layer(layer)

  def bind_layer(self, layer: Layer):
    def check_targets(expression):
      if expression.operation in {'scalar', 'finite'}:
        for item in expression.items:
          name = json.loads(item) if item.startswith('"') else item
          if name not in self.installation.bros:
            raise ValueError(f'bro target {name!r} is not installed')
      for operand in expression.operands:
        check_targets(operand)

    for entry in (*layer.grant, *layer.revoke, *layer.limit):
      if (
        entry.permission.type == 'bro'
        and entry.permission.option == 'target'
        and entry.permission.values is not None
      ):
        check_targets(entry.permission.values)
      for pattern in (*(entry.use or ()), *(entry.give or ())):
        for condition in pattern.conditions:
          if (
            pattern.type == 'bro' and condition.option == 'target' and condition.values is not None
          ):
            check_targets(condition.values)
    return self.syntax.layer(layer)

  def validate(self) -> None:
    installed = self.installation
    owned = [*self.general_layers(), *self.configuration.user]
    own_home = self.configuration.homes.get(self.home)
    if own_home is not None:
      for target, policy in own_home.bros.items():
        if target not in installed.bros:
          raise ValueError(f'~/.bro.json: {policy.source}: bro {target!r} is not installed')
        owned.append(policy)
    for policy in owned:
      self.resolve(policy)
      for kind in policy.picks:
        if kind not in installed.credential_kinds:
          raise self.failure(policy, 'creds', ValueError(f'unknown credential kind {kind!r}'))
      default = policy.config.default_bro
      if default is not None and default not in installed.bros:
        raise self.failure(policy, 'default-bro', ValueError(f'bro {default!r} is not installed'))
      for key in ('harness', 'summon_harness'):
        name = getattr(policy.config, key)
        if name is not None:
          try:
            installed.check_harness(name)
            if name not in installed.harnesses:
              raise ValueError(f'harness {name!r} is not installed')
          except ValueError as error:
            raise self.failure(policy, key.replace('_', '-'), error) from error
      for harness, text in policy.config.llm.items():
        if harness in installed.harnesses:
          try:
            installed.check_provider(providers.parse_recipe(text).provider)
            providers.bind_recipe(text)
          except ValueError as error:
            raise self.failure(policy, f'llm.{harness}', error) from error
    self.operator()

  def operator(self) -> Authority:
    try:
      resolved = self.bind_policy(Policy(source='operator'), self.configuration.operator)
    except ValueError as error:
      raise ValueError(f'~/.bro.json: operator: {error}') from error
    if resolved.hold or resolved.drop:
      raise ValueError('operator caps cannot hold or drop credential kinds')
    return resolved.apply(Authority.top())
