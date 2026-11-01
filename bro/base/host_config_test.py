import json

import pytest

from bro.base import host_config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
  path = tmp_path / 'bro.json'
  monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(path))

  def write(data):
    path.write_text(json.dumps(data))
    return path

  return write


class TestAttachment:
  @pytest.mark.parametrize(
    'fields, message',
    [
      ({}, 'names a checkout path, a git URL, or both'),
      ({'path': 'https://github.com/foo/api.git'}, 'path must be a filesystem path'),
      ({'path': ''}, 'path must be a filesystem path'),
      ({'url': '/home/foo/api'}, 'url must be a git URL'),
    ],
  )
  def test_a_malformed_attachment_is_rejected(self, fields, message):
    with pytest.raises(ValueError, match=message):
      host_config.Attachment(**fields)


class TestToolSelection:
  def test_the_tool_layer_overrides_the_user_layer(self, config_file, tmp_path):
    config_file(
      {
        'user': {
          'creds': ['github+me', 'brog+linear', 'trails+write'],
          'tools': {'bro.trails.rewind': {'creds': ['trails+analyst', 'openai+benchmark']}},
        },
        'homes': {'test-home': {'creds': ['brog+github']}},
      }
    )

    selected = host_config.tool_selection('bro.trails.rewind')

    assert selected.instances == {
      'trails': 'analyst',
      'github': 'me',
      'brog': 'linear',
      'openai': 'benchmark',
    }
    assert selected.layers == {
      'trails': host_config.TOOL_LAYER,
      'github': host_config.USER_LAYER,
      'brog': host_config.USER_LAYER,
      'openai': host_config.TOOL_LAYER,
    }

  def test_unknown_command_uses_the_user_layer(self, config_file):
    config_file({'user': {'creds': ['brog+linear']}})

    assert host_config.tool_selection('other').instances == {'brog': 'linear'}

  def test_a_tools_key_naming_the_invoked_alias_is_rejected(self, config_file):
    config_file({'user': {'tools': {'rewind': {'creds': ['trails+analyst']}}}})

    with pytest.raises(ValueError, match="names 'rewind', an alias of 'bro.trails.rewind'"):
      host_config.tool_selection('bro.trails.rewind', invoked_as='rewind')

  def test_a_retired_top_level_tools_section_names_its_home(self, config_file):
    config_file({'tools': {'bro.trails.rewind': {'creds': ['trails+analyst']}}})

    with pytest.raises(ValueError, match='unknown key.*tools'):
      host_config.tool_selection(None)


@pytest.mark.parametrize('key', ['projects', 'harness', 'summon-harness', 'summon-depth', 'llm'])
def test_retired_top_level_keys_are_refused(key):
  with pytest.raises(ValueError, match=key):
    host_config.parse({key: {}})


@pytest.mark.parametrize('level', ['settings', 'defaults', 'home', 'bro'])
def test_shared_layer_schema(level):
  parsed = host_config.parse_layer_config(
    {
      'harness': 'custom',
      'summon-harness': 'custom',
      'summon-depth': 0,
      'llm': {'custom': 'uninstalled:model,unknown=field'},
      'grant': ['uninstalled'],
    },
    source='example',
    level=level,
  )
  assert parsed.summon_depth == 0
  assert parsed.llm['custom'] == 'uninstalled:model,unknown=field'


@pytest.mark.parametrize(
  ('level', 'key'),
  [
    ('defaults', 'default-bro'),
    ('bro', 'default-bro'),
    ('settings', 'creds'),
    ('defaults', 'creds'),
    ('settings', 'bros'),
    ('bro', 'bros'),
  ],
)
def test_keys_are_restricted_by_layer(level, key):
  with pytest.raises(ValueError, match=key):
    host_config.parse_layer_config({key: []}, source='example', level=level)


@pytest.mark.parametrize('value', [-1, True, '2', 1.5, None])
def test_depth_requires_a_nonnegative_integer(value):
  with pytest.raises(ValueError, match='example.summon-depth.*non-negative'):
    host_config.parse_layer_config({'summon-depth': value}, source='example', level='bro')


@pytest.mark.parametrize(
  'value', [[], {'bro': 3}, {'bro': ''}, {'Bad': 'echo:echo'}, {'bro': ':echo'}]
)
def test_llm_shape_and_grammar_are_checked(value):
  with pytest.raises(ValueError, match='example.llm'):
    host_config.parse_layer_config({'llm': value}, source='example', level='bro')


def test_unrelated_home_values_are_not_parsed():
  parsed = host_config.parse(
    {'homes': {'foreign': {'unknown': 1, 'bros': {'missing': None}}}},
    home='own',
    bro_homes={'installed': 'own'},
  )
  assert parsed.homes == {}


def test_only_installed_foreign_bro_entries_are_shape_checked():
  config = {
    'homes': {
      'foreign': {
        'unknown': 1,
        'bros': {
          'installed': {'llm': {'missing-harness': 'missing-provider:model'}},
          'other': None,
        },
      }
    }
  }
  parsed = host_config.parse(config, home='own', bro_homes={'installed': 'foreign'})
  assert tuple(parsed.homes['foreign'].bros) == ('installed',)
  config['homes']['foreign']['bros']['installed'] = {'llm': []}
  with pytest.raises(ValueError, match='homes.foreign.bros.installed.llm'):
    host_config.parse(config, home='own', bro_homes={'installed': 'foreign'})


def test_ambient_picks_do_not_require_a_launch_installation_marker(
  config_file, monkeypatch, tmp_path
):
  from bro.workspace import installation

  monkeypatch.setattr(installation, 'ENVIRONMENT', tmp_path / 'credential-only-environment')
  config_file({'user': {'creds': ['github+operator']}})
  assert host_config.tool_selection(None).instances == {'github': 'operator'}
