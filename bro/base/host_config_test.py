import json
from pathlib import Path

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


class TestProjectInstances:
  def test_absent_file_selects_nothing(self, tmp_path, monkeypatch):
    monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(tmp_path / 'nope.json'))
    assert host_config.project_instances(tmp_path) == {}

  def test_selection_list_reads_as_kind_to_instance(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path): ['brog+github', 'github+ppp']}})
    assert host_config.project_instances(tmp_path) == {'brog': 'github', 'github': 'ppp'}

  def test_bare_string_is_a_single_selection(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path): 'brog+github'}})
    assert host_config.project_instances(tmp_path) == {'brog': 'github'}

  def test_trailing_plus_selects_the_kinds_own_entry(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path): ['brog+']}})
    assert host_config.project_instances(tmp_path) == {'brog': None}

  def test_other_projects_are_not_this_ones(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path / 'elsewhere'): ['brog+github']}})
    assert host_config.project_instances(tmp_path) == {}

  def test_keys_expand_and_resolve_before_matching(self, config_file, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'repo').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'repo')
    config_file({'projects': {'~/repo': ['brog+github']}})
    assert host_config.project_instances(tmp_path / 'link') == {'brog': 'github'}

  def test_selection_without_a_plus_is_rejected(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path): ['brog']}})
    with pytest.raises(ValueError, match="selection 'brog' names no instance"):
      host_config.project_instances(tmp_path)

  def test_malformed_instance_is_rejected(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path): ['brog+GitHub']}})
    with pytest.raises(ValueError, match='malformed secret name'):
      host_config.project_instances(tmp_path)

  def test_two_selections_of_one_kind_are_rejected(self, config_file, tmp_path):
    config_file({'projects': {str(tmp_path): ['brog+github', 'brog+flow']}})
    with pytest.raises(ValueError, match="selects kind 'brog' twice"):
      host_config.project_instances(tmp_path)

  def test_unknown_top_level_key_is_rejected(self, config_file, tmp_path):
    config_file({'projects': {}, 'credentials': {}})
    with pytest.raises(ValueError, match='unknown key\\(s\\).*credentials'):
      host_config.project_instances(tmp_path)

  def test_another_projects_typo_fails_this_read(self, config_file, tmp_path):
    # the whole file is validated on every read, so a broken entry surfaces at
    # the next launch from any project rather than at the next launch from its own
    config_file({'projects': {str(tmp_path): [], str(tmp_path / 'other'): ['brog']}})
    with pytest.raises(ValueError, match='names no instance'):
      host_config.project_instances(tmp_path)

  def test_non_object_file_is_rejected(self, tmp_path, monkeypatch):
    path = tmp_path / 'bro.json'
    path.write_text('[]')
    monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(path))
    with pytest.raises(ValueError, match='must hold a json object'):
      host_config.project_instances(Path(tmp_path))
