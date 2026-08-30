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


class TestProjectSelection:
  def test_absent_file_selects_nothing(self, tmp_path, monkeypatch):
    monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(tmp_path / 'nope.json'))

    assert host_config.project_selection(str(tmp_path)) == host_config.CredentialSelection({}, {})

  def test_defaults_and_project_merge_with_attribution(self, config_file, tmp_path):
    config_file(
      {
        'defaults': {'creds': ['github+dev', 'trails+write']},
        'projects': {
          str(tmp_path): {'creds': ['github+project', 'brog+']},
        },
      }
    )

    selected = host_config.project_selection(str(tmp_path))

    assert selected.instances == {'github': 'project', 'trails': 'write', 'brog': ''}
    assert selected.layers == {
      'github': host_config.PROJECT_LAYER,
      'trails': host_config.DEFAULTS_LAYER,
      'brog': host_config.PROJECT_LAYER,
    }

  def test_an_unnamed_attachment_gets_defaults_alone(self, config_file, tmp_path):
    config_file(
      {
        'defaults': {'creds': ['github+dev']},
        'projects': {
          str(tmp_path / 'api'): {
            'creds': ['github+api', 'brog+github'],
            'bros': {'reviewer': {'creds': ['trails+review']}},
          }
        },
      }
    )

    assert host_config.project_selection(str(tmp_path / 'elsewhere')).instances == {'github': 'dev'}

  def test_a_detached_launch_gets_defaults_alone(self, config_file, tmp_path):
    config_file(
      {
        'defaults': {'creds': ['github+dev']},
        'projects': {str(tmp_path): {'creds': ['brog+github']}},
      }
    )

    assert host_config.project_selection(None).instances == {'github': 'dev'}

  def test_keys_expand_and_resolve_before_matching(self, config_file, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'repo').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'repo')
    config_file({'projects': {'~/repo': {'creds': ['brog+github']}}})

    assert host_config.project_selection(str(tmp_path / 'link')).instances == {'brog': 'github'}

  def test_a_url_key_matches_the_same_url_normalized(self, config_file):
    config_file({'projects': {'HTTPS://GitHub.com/foo/api.git/': {'creds': ['brog+github']}}})

    selected = host_config.project_selection('https://github.com/foo/api.git')

    assert selected.instances == {'brog': 'github'}

  def test_two_keys_naming_one_attachment_are_rejected(self, config_file):
    config_file(
      {
        'projects': {
          'https://github.com/foo/api.git': {'creds': ['brog+github']},
          'https://GitHub.com/foo/api.git/': {'creds': ['brog+flow']},
        }
      }
    )

    with pytest.raises(ValueError, match='name the same attachment'):
      host_config.project_selection('https://github.com/foo/api.git')


class TestLaunchSelection:
  def test_project_bro_overrides_project_and_defaults(self, config_file, tmp_path):
    config_file(
      {
        'defaults': {'creds': ['github+default', 'trails+write']},
        'projects': {
          str(tmp_path): {
            'creds': ['github+project', 'brog+github'],
            'bros': {
              'reviewer': {'creds': ['github+reviewer', 'trails+review']},
            },
          }
        },
      }
    )

    selected = host_config.launch_selection(str(tmp_path), 'reviewer')

    assert selected.instances == {
      'github': 'reviewer',
      'trails': 'review',
      'brog': 'github',
    }
    assert selected.layers == {
      'github': host_config.PROJECT_BRO_LAYER,
      'trails': host_config.PROJECT_BRO_LAYER,
      'brog': host_config.PROJECT_LAYER,
    }

  def test_an_unlisted_bro_uses_project_and_defaults(self, config_file, tmp_path):
    config_file(
      {
        'defaults': {'creds': ['trails+write']},
        'projects': {str(tmp_path): {'creds': ['github+project']}},
      }
    )

    selected = host_config.launch_selection(str(tmp_path), 'developer')

    assert selected.instances == {'trails': 'write', 'github': 'project'}


class TestToolSelection:
  def test_each_layer_overrides_the_one_above_it(self, config_file, tmp_path):
    config_file(
      {
        'defaults': {'creds': ['trails+write', 'github+dev', 'aws+shared']},
        'user': {
          'creds': ['github+me', 'brog+linear'],
          'tools': {'bro.trails.rewind': {'creds': ['trails+analyst', 'openai+benchmark']}},
        },
        'projects': {str(tmp_path): {'creds': ['brog+github']}},
      }
    )

    selected = host_config.tool_selection('bro.trails.rewind')

    assert selected.instances == {
      'trails': 'analyst',
      'github': 'me',
      'brog': 'linear',
      'aws': 'shared',
      'openai': 'benchmark',
    }
    assert selected.layers == {
      'trails': host_config.TOOL_LAYER,
      'github': host_config.USER_LAYER,
      'brog': host_config.USER_LAYER,
      'aws': host_config.DEFAULTS_LAYER,
      'openai': host_config.TOOL_LAYER,
    }

  def test_unknown_command_uses_the_layers_above_it(self, config_file):
    config_file({'defaults': {'creds': ['github+dev']}, 'user': {'creds': ['brog+linear']}})

    assert host_config.tool_selection('other').instances == {
      'github': 'dev',
      'brog': 'linear',
    }

  def test_a_tools_key_naming_the_invoked_alias_is_rejected(self, config_file):
    config_file({'user': {'tools': {'rewind': {'creds': ['trails+analyst']}}}})

    with pytest.raises(ValueError, match="names 'rewind', an alias of 'bro.trails.rewind'"):
      host_config.tool_selection('bro.trails.rewind', invoked_as='rewind')

  def test_a_retired_top_level_tools_section_names_its_home(self, config_file):
    config_file({'tools': {'bro.trails.rewind': {'creds': ['trails+analyst']}}})

    with pytest.raises(ValueError, match="top-level 'tools' is retired; nest it under 'user'"):
      host_config.tool_selection(None)


class TestValidation:
  def test_trailing_plus_selects_the_empty_instance(self, config_file):
    config_file({'defaults': {'creds': ['brog+']}})

    assert host_config.tool_selection(None).instances == {'brog': ''}

  def test_unknown_kind_is_carried_without_registry_validation(self, config_file):
    config_file({'defaults': {'creds': ['consumer_only+special']}})

    assert host_config.tool_selection(None).instances == {'consumer_only': 'special'}

  def test_retired_instances_field_names_its_replacement(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path): {'instances': ['brog+github']}}})

    with pytest.raises(ValueError, match="'instances' is retired; use 'creds'"):
      host_config.project_selection(str(tmp_path))

  def test_selection_without_a_plus_is_rejected(self, config_file):
    config_file({'defaults': {'creds': ['brog']}})

    with pytest.raises(ValueError, match="selection 'brog' names no instance"):
      host_config.tool_selection(None)

  def test_malformed_instance_is_rejected(self, config_file):
    config_file({'defaults': {'creds': ['brog+GitHub']}})

    with pytest.raises(ValueError, match='malformed secret name'):
      host_config.tool_selection(None)

  def test_two_selections_of_one_kind_are_rejected(self, config_file):
    config_file({'defaults': {'creds': ['brog+github', 'brog+flow']}})

    with pytest.raises(ValueError, match="selects kind 'brog' twice"):
      host_config.tool_selection(None)

  @pytest.mark.parametrize(
    'data, message',
    [
      ({'defaults': []}, 'defaults must hold a json object'),
      ({'defaults': {'creds': 'brog+github'}}, 'creds must be a list'),
      ({'projects': []}, 'projects must be a json object'),
      ({'projects': {'/repo': {'bros': []}}}, 'bros must be a json object'),
      ({'projects': {'/repo': {'bros': {'dev': []}}}}, 'must hold a json object'),
      ({'user': []}, 'user must hold a json object'),
      ({'user': {'tools': []}}, 'tools must be a json object'),
      ({'user': {'tools': {'bro.trails.rewind': []}}}, 'must hold a json object'),
      ({'credentials': {}}, 'unknown key'),
    ],
  )
  def test_bad_shapes_are_rejected(self, config_file, data, message):
    config_file(data)

    with pytest.raises(ValueError, match=message):
      host_config.tool_selection(None)

  def test_every_section_is_validated_on_every_read(self, config_file):
    config_file({'user': {'tools': {'broken.command': {'creds': ['github']}}}})

    with pytest.raises(ValueError, match='names no instance'):
      host_config.llm_presets()

  def test_non_object_file_is_rejected(self, tmp_path, monkeypatch):
    path = tmp_path / 'bro.json'
    path.write_text('[]')
    monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(path))

    with pytest.raises(ValueError, match='must hold a json object'):
      host_config.tool_selection(None)


class TestLLMPresets:
  def test_absent_file_declares_none(self, tmp_path, monkeypatch):
    monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(tmp_path / 'nope.json'))

    assert host_config.llm_presets() == {}

  def test_presets_remain_host_wide(self, config_file):
    config_file({'llm': {'sharp': 'openai:sol:max', 'cheap': ':terra'}})

    assert host_config.llm_presets() == {'sharp': 'openai:sol:max', 'cheap': ':terra'}

  def test_a_non_string_recipe_is_rejected(self, config_file):
    config_file({'llm': {'sharp': 7}})

    with pytest.raises(ValueError, match="preset 'sharp'"):
      host_config.llm_presets()
