"""A root's frozen sources and installation for use/give declarations."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from bro.authority.authority import Authority
from bro.authority.rules import Declaration, Rules
from bro.authority.syntax import Layer, Syntax, parse_layer
from bro.base import configs, credentials, host_config
from bro.base.git_url import canonical_github_url, is_git_url, normalize_git_url
from bro.bro import BaseBro
from bro.harness import Harness, harness_name
from bro.llm import providers
from bro.llm.llm import LLMSpec

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


def _strings(value: object, where: str) -> tuple[str, ...]:
  if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
    raise ValueError(f'{where} must be a list of non-empty strings')
  return tuple(value)


def _positive(value: object, where: str) -> int:
  if type(value) is not int or value <= 0:
    raise ValueError(f'{where} must be a positive integer')
  return value


@dataclass(frozen=True)
class Policy:
  layer: Layer = Layer()
  picks: Mapping[str, str] = field(default_factory=dict)
  recipes: tuple[str, ...] | None = None
  source: str = ''
  repository: bool = False

  def __post_init__(self) -> None:
    object.__setattr__(self, 'picks', MappingProxyType(dict(self.picks)))


def _policy(value: object, where: str, *, bro: bool = False, repository: bool = False) -> Policy:
  allowed = {'grant', 'revoke', 'limit'} | ({'llm', 'creds'} if bro else {'creds'})
  data = _object(value, where, allowed)
  picks = credentials.parse_picks(data.get('creds', []), where=where, field='creds')
  if repository and picks:
    raise ValueError('[tool.bro] cannot choose credential instances')
  recipes = None
  if 'llm' in data:
    recipes = _strings(data['llm'], where + '.llm')
    if not recipes or 'default' in recipes[:-1]:
      raise ValueError(f'{where}: recipes must be non-empty, with default last')
    for recipe in recipes:
      if recipe != 'default':
        providers.parse(recipe)
  return Policy(
    parse_layer(
      grant=_strings(data.get('grant', []), where + '.grant'),
      revoke=_strings(data.get('revoke', []), where + '.revoke'),
      limit=_strings(data.get('limit', []), where + '.limit'),
    ),
    picks,
    recipes,
    where,
    repository,
  )


@dataclass(frozen=True)
class ProjectPolicy:
  policy: Policy
  bros: Mapping[str, Policy]

  def __post_init__(self) -> None:
    object.__setattr__(self, 'bros', MappingProxyType(dict(self.bros)))


def _identity(value: str) -> str:
  if not value:
    raise ValueError('project identity must not be empty')
  if is_git_url(value):
    return canonical_github_url(value) or normalize_git_url(value)
  return str(Path(value).expanduser().resolve())


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
    data = _object(
      value, '~/.bro.json', {'defaults', 'user', 'projects', 'llm', 'summon-depth', 'operator'}
    )
    defaults = _policy(data.get('defaults', {}), 'defaults')
    projects: dict[str, ProjectPolicy] = {}
    for key, entry in _object(data.get('projects', {}), 'projects').items():
      project = _object(entry, f'projects.{key}', {'grant', 'revoke', 'limit', 'creds', 'bros'})
      bros = {
        name: _policy(policy, f'projects.{key}.bros.{name}', bro=True)
        for name, policy in _object(project.get('bros', {}), 'bros').items()
      }
      for name in bros:
        if re.fullmatch(r'[a-z][a-z0-9-]*', name) is None:
          raise ValueError(f'invalid bro name {name!r}')
      identity = _identity(key)
      if identity in projects:
        raise ValueError(f'duplicate project identity {identity!r}')
      projects[identity] = ProjectPolicy(
        _policy({key: item for key, item in project.items() if key != 'bros'}, f'projects.{key}'),
        bros,
      )
    user = _object(data.get('user', {}), 'user', {'creds', 'tools'})
    credentials.parse_picks(user.get('creds', []), where='user', field='creds')
    for name, entry in _object(user.get('tools', {}), 'user.tools').items():
      if not name:
        raise ValueError('tool name must not be empty')
      tool = _object(entry, f'user.tools.{name}', {'creds'})
      credentials.parse_picks(tool.get('creds', []), where=f'user.tools.{name}', field='creds')
    for name, recipe in _object(data.get('llm', {}), 'llm').items():
      if not name or not isinstance(recipe, str):
        raise ValueError('llm presets require names and string recipes')
      providers.parse(recipe)
    if 'summon-depth' in data:
      _positive(data['summon-depth'], 'summon-depth')
    operator = _object(data.get('operator', {}), 'operator', {'revoke', 'limit'})
    layer = parse_layer(
      operator=True,
      **{key: _strings(operator.get(key, []), 'operator.' + key) for key in ('revoke', 'limit')},
    )
    return cls(data, defaults, projects, layer)


@dataclass(frozen=True)
class HarnessReading:
  harness: Harness
  recipes: Mapping[str, LLMSpec]
  auth_kind: str | None = None
  llm_key: bool = False

  def __post_init__(self) -> None:
    object.__setattr__(self, 'recipes', MappingProxyType(dict(self.recipes)))


@dataclass(frozen=True)
class Installation:
  bros: Mapping[str, BaseBro]
  harnesses: Mapping[str, HarnessReading]
  credential_kinds: frozenset[str]
  provider_needs: Mapping[str, frozenset[str]]
  declarations: Mapping[str, Declaration]

  def __post_init__(self) -> None:
    for name in ('bros', 'harnesses', 'provider_needs', 'declarations'):
      object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

  @classmethod
  def take(cls) -> Installation:
    from bro import registry
    from bro.worker_types import installed_types
    from ride.harness import get_harness, installed_harness_names

    kinds = frozenset(credentials.default_registry())
    bros = {
      name: registry.get_class(name)(credential_presence=kinds, credential_kinds=kinds)
      for name in registry.known_names()
    }
    harnesses = {}
    for name in installed_harness_names():
      driver = get_harness(name)
      scope = driver.scope_recipe()
      harnesses[name] = HarnessReading(
        scope.harness,
        {target: driver.resolve_llm(None, target) for target in bros},
        scope.auth_secret,
        scope.llm_key,
      )
    return cls(
      bros,
      harnesses,
      kinds,
      {
        name: frozenset(providers.default_spec(name).needed_secrets())
        for name in providers.known_names()
      },
      {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
    )


@dataclass(frozen=True)
class Reading:
  configuration: Configuration
  store_defaults: Mapping[str, str]
  installation: Installation
  attachment: host_config.Attachment | None = None
  project: Mapping[str, Any] = field(default_factory=dict)
  project_policy: Policy = Policy()

  def __post_init__(self) -> None:
    object.__setattr__(self, 'store_defaults', MappingProxyType(dict(self.store_defaults)))
    object.__setattr__(self, 'project', _freeze(dict(self.project)))
    unknown = self.store_defaults.keys() - self.installation.credential_kinds
    if unknown:
      raise ValueError(f'store defaults name unknown credential kinds: {sorted(unknown)}')

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
    directory = store or configs.STORE_DIR
    index = Path(directory) / credentials.STORE_FILE
    data = _object(json.loads(index.read_text()), str(index)) if index.is_file() else {}
    defaults = credentials.parse_picks(data.get('defaults', []), where=str(index), field='defaults')
    project = {}
    attachment = None
    if repository is not None:
      attachment = attachment_identities(repository.identity)
      content = repository.read_file('pyproject.toml')
      if content is not None:
        project = tomllib.loads(content.decode()).get('tool', {}).get('bro', {})
        _object(project, '[tool.bro]')
        allowed = {
          'default',
          'harness',
          'summon-harness',
          'image-repository',
          'build-context-command',
          'summon-depth',
          'grant',
          'revoke',
          'limit',
          'creds',
        }
        unknown = {
          key
          for key, value in project.items()
          if key not in allowed and not isinstance(value, dict)
        }
        if unknown:
          raise ValueError(f'[tool.bro]: unknown keys {sorted(unknown)}')
        for key in ('harness', 'summon-harness'):
          if key in project:
            harness_name(project[key])
        if 'summon-depth' in project:
          _positive(project['summon-depth'], '[tool.bro].summon-depth')
    policy = _policy(
      {key: project[key] for key in ('grant', 'revoke', 'limit', 'creds') if key in project},
      '[tool.bro]',
      repository=True,
    )
    result = cls(
      configuration, defaults, installation or Installation.take(), attachment, project, policy
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
  def summon_harness(self) -> str:
    return self.project.get('summon-harness', configs.DEFAULT_SUMMON_HARNESS)

  @property
  def summon_depth(self) -> int:
    return self.configuration.raw.get(
      'summon-depth', self.project.get('summon-depth', configs.DEFAULT_SUMMON_DEPTH)
    )

  def layers(self, target: str) -> tuple[Policy, ...]:
    matches = []
    if self.attachment is not None:
      for identity in (self.attachment.url, self.attachment.path):
        if identity is not None:
          match = self.configuration.projects.get(_identity(identity))
          if match is not None:
            matches.append(match)
    return (
      self.project_policy,
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
    if policy.repository:
      for entry in (*policy.layer.grant, *policy.layer.revoke, *policy.layer.limit):
        options = [entry.permission.option]
        options.extend(
          condition.option
          for pattern in (*(entry.use or ()), *(entry.give or ()))
          for condition in pattern.conditions
        )
        if any(option is not None and option.startswith('cred.') for option in options):
          raise ValueError('[tool.bro] cannot name credential instances')
    return self.syntax.layer(policy.layer)

  def operator(self) -> Authority:
    resolved = self.syntax.layer(self.configuration.operator)
    if resolved.hold or resolved.drop:
      raise ValueError('operator caps cannot hold or drop credential kinds')
    return resolved.apply(Authority.top())
