"""Packaged launch settings of an installation's home."""

import importlib.metadata
import importlib.resources
import tomllib

from bro.base.host_config import ConfigLayer, parse_layer_config
from bro.workspace.installation import validate_home

HomeSettings = ConfigLayer


def settings_from_text(content: str, source: str) -> HomeSettings:
  return parse_layer_config(tomllib.loads(content), source=source, level='settings')


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
