import pytest

from bro.workspace import project as workspace_project
from bro.workspace.project import (
  ProjectConfig,
  project_config,
  project_config_from_text,
  project_sections,
)


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
  monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
  monkeypatch.setattr(workspace_project, 'find_project_root', lambda: tmp_path)
  return tmp_path


def test_absent_project_configuration_is_empty(project_dir):
  assert project_config() == ProjectConfig()
  assert project_sections() == {}
  (project_dir / 'pyproject.toml').write_text('[project]\nname = "example"\n')
  assert project_config() == ProjectConfig()


def test_detached_sections_are_empty(monkeypatch):
  monkeypatch.setattr(workspace_project, 'find_project_root', lambda: None)
  assert project_sections() == {}


def test_tree_reads_only_build_context_and_subtables(project_dir):
  (project_dir / 'pyproject.toml').write_text(
    '[tool.bro]\ndefault = 5\nharness = false\nsummon-harness = "Old"\n'
    'summon-depth = -1\ngrant = ["invalid"]\nimage-repository = 9\n'
    'future-key = true\nbuild-context-command = "list-files"\n'
    '[tool.bro.devoops]\ntarget-registry = "example.targets:registry"\n'
  )
  config = project_config()
  assert config.build_context_command == 'list-files'
  assert (
    config.sections
    == project_sections()
    == {'devoops': {'target-registry': 'example.targets:registry'}}
  )


@pytest.mark.parametrize('value', ['5', '""'])
def test_build_context_command_requires_a_nonempty_string(value):
  with pytest.raises(ValueError, match='build-context-command.*non-empty string'):
    project_config_from_text(f'[tool.bro]\nbuild-context-command = {value}', 'fixture.toml')
