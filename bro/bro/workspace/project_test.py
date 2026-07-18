import pytest

import cw.project
from cw.project import ProjectConfig, project_config


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
  monkeypatch.setattr(cw.project, '_project_root', lambda: tmp_path)
  return tmp_path


class TestProjectConfig:
  def test_missing_pyproject_yields_defaults(self, project_dir):
    assert project_config() == ProjectConfig(persona='ppp-dev', image_repository='bro/ppp-dev')

  def test_missing_table_yields_defaults(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[project]\nname = "x"\n')
    assert project_config() == ProjectConfig(persona='ppp-dev', image_repository='bro/ppp-dev')

  def test_image_repository_derives_from_persona(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\npersona = "kap-dev"\n')
    assert project_config() == ProjectConfig(persona='kap-dev', image_repository='bro/kap-dev')

  def test_explicit_image_repository_wins_over_derivation(self, project_dir):
    (project_dir / 'pyproject.toml').write_text(
      '[tool.bro]\npersona = "kap-dev"\nimage-repository = "custom-images"\n'
    )
    assert project_config() == ProjectConfig(persona='kap-dev', image_repository='custom-images')

  def test_unknown_key_raises(self, project_dir):
    (project_dir / 'pyproject.toml').write_text('[tool.bro]\nimage = "x"\n')
    with pytest.raises(ValueError, match=r'unknown \[tool.bro\] key'):
      project_config()
