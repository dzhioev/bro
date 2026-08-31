import pytest

import bro.workspace.project as workspace_project
from bro.base import configs
from bro.workspace.project import ProjectConfig, project_config, project_sections


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
  monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
  monkeypatch.setattr(workspace_project, 'find_project_root', lambda: tmp_path)
  return tmp_path


@pytest.fixture
def no_project(monkeypatch):
  monkeypatch.setattr(workspace_project, 'find_project_root', lambda: None)


class TestProjectConfig:
  def test_missing_pyproject_raises(self, project_dir):
    with pytest.raises(ValueError, match='missing .*pyproject.toml'):
      project_config()

  def test_missing_table_raises(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[project]\nname = "x"\n')
    with pytest.raises(ValueError, match=r'missing \[tool.bro\] default'):
      project_config()

  def test_image_repository_derives_from_the_default_bro(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\n')
    assert project_config() == ProjectConfig(default_bro='foo', image_repository='bro/foo')

  def test_explicit_image_repository_wins_over_derivation(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\nimage-repository = "custom-images"\n'
    )
    assert project_config() == ProjectConfig(default_bro='foo', image_repository='custom-images')

  def test_sections_default_to_empty(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\n')
    assert project_config().sections == {}

  def test_a_sub_table_is_carried_verbatim(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\n\n[tool.bro.analyst]\nreports = "docs/analyses"\n'
    )
    assert project_config().sections == {'analyst': {'reports': 'docs/analyses'}}

  def test_a_sub_table_key_is_not_a_launch_key(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\n\n[tool.bro.whoever]\nanything = 5\n'
    )
    assert project_config().sections['whoever'] == {'anything': 5}

  def test_harness_defaults_to_claude(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\n')
    assert project_config().harness == 'claude'

  def test_harness_parses(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\nharness = "bro"\n')
    assert project_config().harness == 'bro'

  @pytest.mark.parametrize('value', ['"other"', '5', '""'])
  def test_harness_must_be_supported(self, project_dir, value):
    (project_dir / 'pyproject.toml').write_text(f'[tool.bro]\ndefault = "foo"\nharness = {value}\n')
    with pytest.raises(ValueError, match=r'\[tool.bro\] harness'):
      project_config()

  def test_build_context_command_defaults_to_none(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\n')
    assert project_config().build_context_command is None

  def test_build_context_command_parses(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\nbuild-context-command = "list-files"\n'
    )
    assert project_config().build_context_command == 'list-files'

  @pytest.mark.parametrize('value', ['5', '""'])
  def test_build_context_command_must_be_a_non_empty_string(self, project_dir, value):
    (project_dir / 'pyproject.toml').write_text(
      f'[tool.bro]\ndefault = "foo"\nbuild-context-command = {value}\n'
    )
    with pytest.raises(ValueError, match='build-context-command .* non-empty string'):
      project_config()

  def test_summon_depth_defaults_and_parses(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\n')
    assert project_config().summon_depth == configs.DEFAULT_SUMMON_DEPTH

    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\nsummon-depth = 5\n')
    assert project_config().summon_depth == 5

  @pytest.mark.parametrize('value', ['0', '-1', '2.5', 'true', '"3"'])
  def test_summon_depth_must_be_a_positive_integer(self, project_dir, value):
    (project_dir / 'pyproject.toml').write_text(
      f'[tool.bro]\ndefault = "foo"\nsummon-depth = {value}\n'
    )
    with pytest.raises(ValueError, match='summon-depth .* positive integer'):
      project_config()

  def test_unknown_key_raises(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\nimage = "x"\n')
    with pytest.raises(ValueError, match=r'unknown \[tool.bro\] key'):
      project_config()

  def test_persona_key_is_unknown(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\npersona = "foo"\n')
    with pytest.raises(ValueError, match=r'unknown \[tool.bro\] key.*persona'):
      project_config()

  def test_default_must_be_a_string(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = 5\n')
    with pytest.raises(ValueError, match=r'\[tool.bro\] default .* must be a string'):
      project_config()


class TestProjectSections:
  def test_a_sub_table_is_carried_verbatim(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\n\n[tool.bro.llm]\nsharp = "openai:sol:max"\n'
    )
    assert project_sections() == {'llm': {'sharp': 'openai:sol:max'}}

  def test_no_project_carries_no_sections(self, no_project):
    assert project_sections() == {}

  def test_a_project_without_the_launch_keys_still_carries_its_sections(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro.llm]\nsharp = "openai:sol:max"\n')

    assert project_sections() == {'llm': {'sharp': 'openai:sol:max'}}
    with pytest.raises(ValueError, match=r'missing \[tool.bro\] default'):
      project_config()

  def test_a_project_with_no_pyproject_carries_no_sections(self, project_dir):
    assert project_sections() == {}
