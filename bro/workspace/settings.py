"""Packaged launch settings of an installation's home."""

import importlib.metadata
import importlib.resources
import tomllib
from dataclasses import dataclass
from typing import Optional

from bro.authority.syntax import parse_layer
from bro.base import configs
from bro.harness import harness_name
from bro.workspace.installation import validate_home


@dataclass(frozen=True)
class HomeSettings:
  default_bro: str | None = None
  harness: str | None = None
  summon_harness: str | None = None
  summon_depth: int = configs.DEFAULT_SUMMON_DEPTH
  grant: tuple[str, ...] = ()
  revoke: tuple[str, ...] = ()
  limit: tuple[str, ...] = ()


def _optional_string(table: dict, source: str, key: str) -> Optional[str]:
  value = table.get(key)
  if value is not None and (not isinstance(value, str) or value == ''):
    raise ValueError(f'{key} in {source} must be a non-empty string')
  return value


def _harness(table: dict, source: str, key: str) -> Optional[str]:
  value = _optional_string(table, source, key)
  if value is None:
    return None
  try:
    return harness_name(value)
  except ValueError as error:
    raise ValueError(f'{key} in {source}: {error}') from error


def _positive_integer(value: object, source: str, key: str) -> int:
  if type(value) is not int or value <= 0:
    raise ValueError(f'{key} in {source} must be a positive integer')
  return value


def _scope_values(table: dict, source: str, key: str) -> tuple[str, ...]:
  values = table.get(key, [])
  if not isinstance(values, list):
    raise ValueError(f'{key} in {source} must be a list')
  if not all(isinstance(value, str) and value != '' for value in values):
    raise ValueError(f'{key} in {source} must contain non-empty strings')
  return tuple(values)


def settings_from_text(content: str, source: str) -> HomeSettings:
  table = tomllib.loads(content)
  allowed = {'default-bro', 'harness', 'summon-harness', 'summon-depth', 'grant', 'revoke', 'limit'}
  if unknown := table.keys() - allowed:
    raise ValueError(f'unknown home settings keys in {source}: {sorted(unknown)}')
  grant = _scope_values(table, source, 'grant')
  revoke = _scope_values(table, source, 'revoke')
  limit = _scope_values(table, source, 'limit')
  try:
    parse_layer(grant=grant, revoke=revoke, limit=limit)
  except ValueError as error:
    raise ValueError(f'{source}: {error}') from error
  return HomeSettings(
    default_bro=_optional_string(table, source, 'default-bro'),
    harness=_harness(table, source, 'harness'),
    summon_harness=_harness(table, source, 'summon-harness'),
    summon_depth=_positive_integer(
      table.get('summon-depth', configs.DEFAULT_SUMMON_DEPTH), source, 'summon-depth'
    ),
    grant=grant,
    revoke=revoke,
    limit=limit,
  )


def home_settings(home: str) -> HomeSettings:
  validate_home(home)
  entries: dict[str, importlib.metadata.EntryPoint] = {}
  for entry in importlib.metadata.entry_points(group='bro.settings'):
    validate_home(entry.name)
    if entry.name in entries:
      previous = entries[entry.name]
      assert previous.dist is not None and entry.dist is not None
      raise ValueError(
        f'duplicate settings for home {entry.name!r}: '
        f'{previous.dist.metadata["Name"]} vs {entry.dist.metadata["Name"]}'
      )
    entries[entry.name] = entry
  entry = entries.get(home)
  if entry is None:
    return HomeSettings()
  resource = importlib.resources.files(entry.value).joinpath('bro-settings.toml')
  return settings_from_text(resource.read_text(), str(resource))
