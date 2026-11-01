"""The optional host configuration, shared by installations of several homes.

`defaults` and `homes.<home>` layer over packaged home settings; each home may
hold `bros.<bro>` layers. Recipes are `llm` maps keyed by harness. A reading
parses its own home in full and only the foreign bro entries it installs.
`user.creds` and `user.tools.<canonical command>.creds` select credentials for
ambient commands, independently of session policy. `operator` restricts starts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from bro.authority.syntax import Layer, parse_layer
from bro.base import configs, credentials
from bro.base.args import CLIError
from bro.base.git_url import is_git_url
from bro.llm.providers import parse_recipe

HOST_CONFIG_FILE = configs.DEFAULT_HOST_CONFIG
USER_LAYER = 'user'
TOOL_LAYER = 'user.tools'


@dataclass(frozen=True)
class Attachment:
  """An attached repository's checkout path, git URL, or both."""

  path: Optional[str] = None
  url: Optional[str] = None

  def __post_init__(self) -> None:
    if self.path is None and self.url is None:
      raise ValueError('an attachment names a checkout path, a git URL, or both')
    if self.path is not None and (self.path == '' or is_git_url(self.path)):
      raise ValueError(f'attachment path must be a filesystem path, not {self.path!r}')
    if self.url is not None and not is_git_url(self.url):
      raise ValueError(f'attachment url must be a git URL, not {self.url!r}')


@dataclass(frozen=True)
class ConfigLayer:
  default_bro: str | None = None
  harness: str | None = None
  summon_harness: str | None = None
  summon_depth: int | None = None
  llm: Mapping[str, str] = field(default_factory=dict)
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  limit: tuple[str, ...] = ()
  creds: tuple[str, ...] = ()

  def __post_init__(self) -> None:
    object.__setattr__(self, 'llm', MappingProxyType(dict(self.llm)))

  @property
  def selection(self) -> dict[str, str]:
    return credentials.parse_picks(list(self.creds), where='configuration', field='creds')


@dataclass(frozen=True)
class Home:
  layer: ConfigLayer
  bros: Mapping[str, ConfigLayer]

  def __post_init__(self) -> None:
    object.__setattr__(self, 'bros', MappingProxyType(dict(self.bros)))


@dataclass(frozen=True)
class CredentialSelection:
  instances: dict[str, str]
  layers: dict[str, str]


@dataclass(frozen=True)
class _User:
  credentials: dict[str, str]
  tools: dict[str, dict[str, str]]


@dataclass(frozen=True)
class Configuration:
  defaults: ConfigLayer
  homes: Mapping[str, Home]
  user: _User
  operator: Layer


def name(value: object, where: str) -> str:
  if not isinstance(value, str) or re.fullmatch(r'[a-z][a-z0-9-]*', value) is None:
    raise ValueError(f'{where}: invalid name {value!r}; expected [a-z][a-z0-9-]*')
  return value


def _object(value: object, where: str) -> dict:
  if not isinstance(value, dict):
    raise ValueError(f'{where} must be an object')
  return value


def _reject_unknown_fields(value: dict, allowed: set[str], where: str) -> None:
  if unknown := value.keys() - allowed:
    raise ValueError(f'{where}: unknown key(s): {", ".join(sorted(unknown))}')


def _scope_values(where: str, values: object, key: str) -> tuple[str, ...]:
  if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
    raise ValueError(f'{where}.{key} must be a list of non-empty strings')
  return tuple(values)


def parse_layer_config(value: object, *, source: str, level: str) -> ConfigLayer:
  table = _object(value, source)
  allowed = {'harness', 'summon-harness', 'summon-depth', 'llm', 'grant', 'revoke', 'limit'}
  if level in {'settings', 'home'}:
    allowed.add('default-bro')
  if level in {'home', 'bro'}:
    allowed.add('creds')
  _reject_unknown_fields(table, allowed, source)
  names = {
    key.replace('-', '_'): name(table[key], f'{source}.{key}')
    for key in ('default-bro', 'harness', 'summon-harness')
    if key in table
  }
  depth = table.get('summon-depth')
  if 'summon-depth' in table and (type(depth) is not int or depth < 0):
    raise ValueError(f'{source}.summon-depth must be a non-negative integer')
  recipes = _object(table.get('llm', {}), f'{source}.llm')
  for harness, text in recipes.items():
    name(harness, f'{source}.llm')
    try:
      if not isinstance(text, str):
        raise ValueError('recipe must be a string')
      parse_recipe(text)
    except (TypeError, ValueError) as error:
      raise ValueError(f'{source}.llm.{harness}: {error}') from error
  scope = {
    key: _scope_values(source, table.get(key, []), key)
    for key in ('grant', 'revoke', 'limit', 'creds')
  }
  try:
    parse_layer(grant=scope['grant'], revoke=scope['revoke'], limit=scope['limit'])
    credentials.parse_picks(list(scope['creds']), where=source, field='creds')
  except ValueError as error:
    raise ValueError(f'{source}: {error}') from error
  return ConfigLayer(
    default_bro=names.get('default_bro'),
    harness=names.get('harness'),
    summon_harness=names.get('summon_harness'),
    summon_depth=depth,
    llm=dict(recipes),
    grant=scope['grant'],
    revoke=scope['revoke'],
    limit=scope['limit'],
    creds=scope['creds'],
  )


def parse(
  value: object,
  *,
  source: str = '~/.bro.json',
  home: str | None = None,
  bro_homes: Mapping[str, str] | None = None,
) -> Configuration:
  bro_homes = {} if bro_homes is None else bro_homes
  table = _object(value, source)
  _reject_unknown_fields(table, {'defaults', 'homes', 'user', 'operator'}, source)
  defaults = parse_layer_config(
    table.get('defaults', {}), source=f'{source}: defaults', level='defaults'
  )
  raw_homes = _object(table.get('homes', {}), f'{source}: homes')
  for key in raw_homes:
    name(key, f'{source}: homes')
  homes = {}
  if home is not None and home in raw_homes:
    where = f'{source}: homes.{home}'
    owned = _object(raw_homes[home], where)
    layer = parse_layer_config(
      {key: entry for key, entry in owned.items() if key != 'bros'}, source=where, level='home'
    )
    bros = _object(owned.get('bros', {}), f'{where}.bros')
    homes[home] = Home(
      layer,
      {
        name(bro, f'{where}.bros'): parse_layer_config(
          entry, source=f'{where}.bros.{bro}', level='bro'
        )
        for bro, entry in bros.items()
      },
    )
  for bro, foreign in bro_homes.items():
    if foreign == home or foreign not in raw_homes:
      continue
    section = _object(raw_homes[foreign], f'{source}: homes.{foreign}')
    bros = _object(section.get('bros', {}), f'{source}: homes.{foreign}.bros')
    if bro in bros:
      parsed = parse_layer_config(
        bros[bro], source=f'{source}: homes.{foreign}.bros.{bro}', level='bro'
      )
      previous = homes.get(foreign, Home(ConfigLayer(), {}))
      homes[foreign] = Home(previous.layer, {**previous.bros, bro: parsed})
  operator = _object(table.get('operator', {}), f'{source}: operator')
  _reject_unknown_fields(operator, {'revoke', 'limit'}, f'{source}: operator')
  return Configuration(
    defaults,
    homes,
    _user(Path(source), table.get('user', {})),
    parse_layer(
      revoke=_scope_values(f'{source}: operator', operator.get('revoke', []), 'revoke'),
      limit=_scope_values(f'{source}: operator', operator.get('limit', []), 'limit'),
      operator=True,
    ),
  )


def _read() -> Configuration:
  path = Path(HOST_CONFIG_FILE)
  try:
    return parse(json.loads(path.read_text()) if path.is_file() else {}, source=str(path))
  except json.JSONDecodeError as error:
    raise CLIError(f'{path} is not valid json') from error
  except (TypeError, ValueError) as error:
    raise CLIError(f'{path}: {error}') from error


def tool_selection(
  command: Optional[str], *, invoked_as: Optional[str] = None
) -> CredentialSelection:
  config = _read()
  if command is not None and (not isinstance(command, str) or not command):
    raise ValueError('command name must be a non-empty string')
  if (
    command is not None
    and invoked_as is not None
    and invoked_as != command
    and invoked_as in config.user.tools
  ):
    raise ValueError(
      f'{HOST_CONFIG_FILE}: user.tools names {invoked_as!r}, an alias of {command!r}; key it by the canonical name'
    )
  instances = dict(config.user.credentials)
  layers = dict.fromkeys(instances, USER_LAYER)
  if command is not None:
    picked = config.user.tools.get(command, {})
    instances.update(picked)
    layers.update(dict.fromkeys(picked, TOOL_LAYER))
  return CredentialSelection(instances, layers)


def _user(path: Path, value: object) -> _User:
  where = f'{path}: {"user"}'
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {'creds', 'tools'}, where)
  tools = value.get('tools', {})
  if not isinstance(tools, dict):
    raise ValueError(f'{where}: {"tools"} must be a json object')
  parsed_tools: dict[str, dict[str, str]] = {}
  for name, entry in tools.items():
    if name == '':
      raise ValueError(f'{where}: command name must not be empty')
    parsed_tools[name] = _selection_object(path, f'{where}: command {name!r}', entry)
  return _User(_selection_entries(where, value.get('creds', [])), parsed_tools)


def _selection_object(path: Path, subject: str, value: object) -> dict[str, str]:
  where = f'{path}: {subject}' if not subject.startswith(f'{path}:') else subject
  if not isinstance(value, dict):
    raise ValueError(f'{where} must hold a json object')
  _reject_unknown_fields(value, {'creds'}, where)
  return _selection_entries(where, value.get('creds', []))


def _selection_entries(where: str, entries: object) -> dict[str, str]:
  return credentials.parse_picks(entries, where=where, field='creds')
