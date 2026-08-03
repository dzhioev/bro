import pytest

import bro.workspace.project as workspace_project
from bro.workspace.project import ProjectConfig, project_config


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
  monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
  return tmp_path


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

  def test_footer_command_defaults_to_none(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\n')
    assert project_config().footer_command is None

  def test_footer_command_parses(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\nfooter-command = "repo-footer"\n'
    )
    assert project_config().footer_command == 'repo-footer'

  @pytest.mark.parametrize('value', ['5', '""'])
  def test_footer_command_must_be_a_non_empty_string(self, project_dir, value):
    (project_dir / 'pyproject.toml').write_text(
      f'[tool.bro]\ndefault = "foo"\nfooter-command = {value}\n'
    )
    with pytest.raises(ValueError, match='footer-command .* non-empty string'):
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

  def test_creds_default_to_empty(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\n')
    assert project_config().creds == {}

  def test_creds_parse_as_kind_to_instance(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\ncreds = { brog = "github", github = "master" }\n'
    )
    assert project_config().creds == {'brog': 'github', 'github': 'master'}

  def test_creds_instance_outside_the_name_grammar_raises(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\ncreds = { brog = "GitHub" }\n'
    )
    with pytest.raises(ValueError, match=r"creds entry 'brog' = 'GitHub'"):
      project_config()

  def test_creds_kind_with_instance_marker_raises(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\ncreds = { "brog+x" = "github" }\n'
    )
    with pytest.raises(ValueError, match=r"creds entry 'brog\+x'"):
      project_config()

  def test_creds_non_string_instance_raises(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\ncreds = { brog = 5 }\n'
    )
    with pytest.raises(ValueError, match='instance must be a string'):
      project_config()

  def test_creds_non_table_raises(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\ndefault = "foo"\ncreds = "brog"\n')
    with pytest.raises(ValueError, match='must be a table'):
      project_config()
