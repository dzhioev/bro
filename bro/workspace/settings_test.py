import importlib.metadata
from unittest.mock import Mock

import pytest

from bro.workspace.settings import HomeSettings, home_settings, settings_from_text


def test_a_home_without_settings_is_empty():
  assert home_settings('unconfigured') == HomeSettings()


def test_packaged_settings_load_only_the_installation_home(home_settings_file):
  home_settings_file.write_text(
    'default-bro = "example"\nharness = "custom"\nsummon-harness = "another"\nsummon-depth = 4\ngrant = ["@reviewer"]\n'
  )
  settings = home_settings('test-home')
  assert settings.default_bro == 'example'
  assert settings.harness == 'custom'
  assert settings.summon_harness == 'another'
  assert settings.summon_depth == 4
  assert settings.grant == ('@reviewer',)
  home_settings_file.write_text('broken')
  assert home_settings('another-home') == HomeSettings()


@pytest.mark.parametrize('key', ['harness', 'summon-harness', 'default-bro'])
@pytest.mark.parametrize('value', ['5', '""'])
def test_string_keys_are_validated_with_the_source(key, value):
  with pytest.raises(ValueError, match=f'{key}.*fixture.toml'):
    settings_from_text(f'{key} = {value}', 'fixture.toml')


@pytest.mark.parametrize('key', ['harness', 'summon-harness'])
def test_harness_names_are_validated_without_loading_them(key):
  with pytest.raises(ValueError, match=key):
    settings_from_text(f'{key} = "Wrong"', 'fixture.toml')


@pytest.mark.parametrize('value', ['0', '-1', 'true', '"3"', '2.5'])
def test_depth_is_a_positive_integer(value):
  with pytest.raises(ValueError, match='summon-depth.*positive integer'):
    settings_from_text(f'summon-depth = {value}', 'fixture.toml')


@pytest.mark.parametrize('key', ['grant', 'revoke', 'limit'])
@pytest.mark.parametrize('value', ['"@reviewer"', '[7]', '[""]'])
def test_scope_lists_require_nonempty_strings(key, value):
  with pytest.raises(ValueError, match=key):
    settings_from_text(f'{key} = {value}', 'fixture.toml')


@pytest.mark.parametrize(
  'entry', ['grant = [":launch.bro"]', 'limit = ["@reviewer"]', 'default = "old"']
)
def test_invalid_settings_name_their_source(entry):
  with pytest.raises(ValueError, match='fixture.toml'):
    settings_from_text(entry, 'fixture.toml')


def test_duplicate_settings_refuse_even_an_unrelated_home(monkeypatch):
  entries = []
  for distribution in ('first-distribution', 'second-distribution'):
    entry = importlib.metadata.EntryPoint('same', 'unimportable.package', 'bro.settings')
    object.__setattr__(entry, 'dist', Mock(metadata={'Name': distribution}))
    entries.append(entry)
  monkeypatch.setattr(importlib.metadata, 'entry_points', lambda **kwargs: entries)
  with pytest.raises(ValueError, match='first-distribution.*second-distribution'):
    home_settings('unrelated')
