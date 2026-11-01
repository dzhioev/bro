"""A root's frozen sources and installation for use/give declarations."""

from __future__ import annotations

import json
import re
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
from bro.llm.llm import Recipe
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
  layer: Layer = Layer()
  picks: Mapping[str, str] = field(default_factory=dict)
  recipes: tuple[str, ...] | None = None
  source: str = ''
  home_settings: bool = False

  def __post_init__(self) -> None:
    object.__setattr__(self, 'picks', MappingProxyType(dict(self.picks)))


@dataclass(frozen=True)
class ProjectPolicy:
  policy: Policy
  bros: Mapping[str, Policy]

  def __post_init__(self) -> None:
    object.__setattr__(self, 'bros', MappingProxyType(dict(self.bros)))


@dataclass(frozen=True)
class Configuration:
  raw: Mapping[str, Any]
  defaults: Policy
  projects: Mapping[str, ProjectPolicy]
  operator: Layer

  def __post_init__(self) -> None:
    object.__setattr__(self, 'raw', _freeze(dict(self.raw)))
    object.__setattr__(self, 'projects', MappingProxyType(dict(self.projects)))

  @classmethod
  def parse(cls, value: object) -> Configuration:
    parsed = host_config.parse(value)

    def policy(entry: host_config.ScopeEntry, source: str) -> Policy:
      scope = entry.scope
      recipes = entry.llm if isinstance(entry, host_config.BroEntry) else None
      for recipe in recipes or ():
        if recipe != 'default':
          providers.parse(recipe)
      return Policy(
        parse_layer(grant=scope.grant, revoke=scope.revoke, limit=scope.limit),
        entry.selection,
        recipes,
        source,
      )

    projects = {
      identity: ProjectPolicy(
        policy(project.entry, f'projects.{identity}'),
        {
          name: policy(entry, f'projects.{identity}.bros.{name}')
          for name, entry in project.bros.items()
        },
      )
      for identity, project in parsed.projects.items()
    }
    for project in projects.values():
      for name in project.bros:
        if re.fullmatch(r'[a-z][a-z0-9-]*', name) is None:
          raise ValueError(f'invalid bro name {name!r}')
    for name, recipe in parsed.llm.items():
      if not name:
        raise ValueError('llm presets require non-empty names')
      providers.parse(recipe)
    return cls(
      cast(dict[str, Any], value), policy(parsed.defaults, 'defaults'), projects, parsed.operator
    )


@dataclass(frozen=True)
class HarnessReading:
  harness: Harness
  recipes: Mapping[str, Recipe | None]
  auth_kind: str | None = None
  llm_key: bool = False
  unavailable_recipes: Mapping[str, str] = field(default_factory=dict)

  def __post_init__(self) -> None:
    object.__setattr__(self, 'recipes', MappingProxyType(dict(self.recipes)))
    object.__setattr__(
      self, 'unavailable_recipes', MappingProxyType(dict(self.unavailable_recipes))
    )


@dataclass(frozen=True)
class Installation:
  bros: Mapping[str, BaseBro]
  harnesses: Mapping[str, HarnessReading]
  credential_kinds: frozenset[str]
  provider_needs: Mapping[str, frozenset[str]]
  declarations: Mapping[str, Declaration]
  unavailable_harnesses: Mapping[str, str] = field(default_factory=dict)
  unavailable_bros: Mapping[str, str] = field(default_factory=dict)
  unavailable_providers: Mapping[str, str] = field(default_factory=dict)

  def __post_init__(self) -> None:
    for name in (
      'bros',
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
      recipes: dict[str, Recipe | None] = {}
      unavailable_recipes: dict[str, str] = {}
      for target, bro in bros.items():
        if scope.llm_key and bro.llm_spec is None:
          recipes[target] = None
          continue
        try:
          recipes[target] = driver.resolve_llm(None, target)
        except Exception as error:
          unavailable_recipes[target] = str(error)
          log.warning('bro %r recipe on harness %r is unavailable: %s', target, name, error)
      harnesses[name] = HarnessReading(
        scope.harness, recipes, scope.auth_secret, scope.llm_key, unavailable_recipes
      )
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

    configuration = Configuration.parse(
      json.loads((host or Path(host_config.HOST_CONFIG_FILE)).read_text())
      if (host or Path(host_config.HOST_CONFIG_FILE)).is_file()
      else {}
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
    policy = Policy(
      parse_layer(grant=settings.grant, revoke=settings.revoke, limit=settings.limit),
      source=f'{home} home settings',
      home_settings=True,
    )
    result = cls(
      configuration,
      defaults,
      installation or Installation.take(),
      attachment,
      project,
      policy,
      settings,
      home,
    )
    result.resolve(policy)
    return result

  @property
  def syntax(self) -> Syntax:
    return Syntax(self.installation.declarations, self.installation.credential_kinds)

  @property
  def rules(self) -> Rules:
    return Rules(self.installation.declarations)

  @property
  def default_bro(self) -> str:
    if self.settings.default_bro is None:
      raise ValueError('no default bro: pass a bro or set default-bro in home settings')
    return self.settings.default_bro

  @property
  def harness(self) -> str | None:
    return self.settings.harness or self.configuration.raw.get('harness')

  @property
  def summon_harness(self) -> str | None:
    return self.settings.summon_harness or self.configuration.raw.get('summon-harness')

  def resolve_harness(self, selected: str | None = None, *, summon: bool = False) -> str:
    name = selected if selected is not None else self.summon_harness if summon else self.harness
    if name is None:
      key = 'summon-harness' if summon else 'harness'
      raise ValueError(
        f'no harness: pass --harness, set home settings {key}, or set "{key}" in ~/.bro.json'
      )
    return name

  @property
  def summon_depth(self) -> int:
    return self.configuration.raw.get('summon-depth', self.settings.summon_depth)

  def layers(self, target: str) -> tuple[Policy, ...]:
    matches = []
    if self.attachment is not None:
      for identity in (self.attachment.url, self.attachment.path):
        if identity is not None:
          match = self.configuration.projects.get(host_config.project_identity(identity))
          if match is not None:
            matches.append(match)
    return (
      self.home_policy,
      self.configuration.defaults,
      *(match.policy for match in matches),
      *(match.bros[target] for match in matches if target in match.bros),
    )

  def picks(self, target: str) -> dict[str, str]:
    result = dict(self.store_defaults)
    for policy in self.layers(target):
      result.update(policy.picks)
    return result

  def recipes(self, target: str) -> tuple[str, ...]:
    return next(
      (policy.recipes for policy in reversed(self.layers(target)) if policy.recipes is not None),
      ('default',),
    )

  def resolve(self, policy: Policy):
    unknown = policy.picks.keys() - self.installation.credential_kinds
    if unknown:
      raise ValueError(f'{policy.source}: unknown credential kinds {sorted(unknown)}')
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
    return self.syntax.layer(policy.layer)

  def operator(self) -> Authority:
    resolved = self.syntax.layer(self.configuration.operator)
    if resolved.hold or resolved.drop:
      raise ValueError('operator caps cannot hold or drop credential kinds')
    return resolved.apply(Authority.top())
