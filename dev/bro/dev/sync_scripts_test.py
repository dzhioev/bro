import subprocess
import sys
import tomllib

from bro.dev import sync_scripts


def test_imports_without_git_metadata(tmp_path):
  result = subprocess.run(
    [sys.executable, '-c', 'import bro.dev.sync_scripts'],
    capture_output=True,
    text=True,
    cwd=tmp_path,
  )
  assert result.returncode == 0, f'stderr: {result.stderr}'


def test_syncs_one_project_without_scanning_the_ones_nested_in_it(tmp_path):
  (tmp_path / 'package').mkdir()
  (tmp_path / 'package' / '__init__.py').write_text('')
  (tmp_path / 'package' / 'cli.py').write_text(
    "__cli_name__ = 'tool'\n\ndef main(argv):\n  return argv\n"
  )
  (tmp_path / 'top.py').write_text('def main(argv):\n  return argv\n')
  (tmp_path / 'conftest.py').write_text('VALUE = 1\n')
  (tmp_path / '_entrypoints.py').write_text('stale\n')
  (tmp_path / 'member').mkdir()
  (tmp_path / 'member' / 'foreign.py').write_text('def main(argv):\n  return argv\n')
  (tmp_path / 'member' / 'pyproject.toml').write_text('[project]\nname = "member"\n')
  (tmp_path / 'beside').mkdir()
  (tmp_path / 'beside' / 'unrelated.py').write_text('def main(argv):\n  return argv\n')
  (tmp_path / 'beside' / 'pyproject.toml').write_text('[project]\nname = "beside"\n')
  (tmp_path / 'pyproject.toml').write_text(
    """[project]
name = "example"
version = "0.1.0"
[project.scripts]
stale = "_entrypoints:stale"
[tool.uv.workspace]
members = ["member"]
"""
  )

  project = sync_scripts._project(tmp_path)
  sync_scripts.sync_pyproject(project)
  sync_scripts.sync_entrypoints(project)

  data = tomllib.loads((tmp_path / 'pyproject.toml').read_text())
  assert data['project']['scripts'] == {
    'package.cli': '_entrypoints:package_cli',
    'tool': '_entrypoints:package_cli',
    'top': '_entrypoints:top',
  }
  bridge = (tmp_path / '_entrypoints.py').read_text()
  assert "_run('package.cli')" in bridge
  assert "_run('top')" in bridge
  assert 'foreign' not in bridge
  assert 'unrelated' not in bridge
  assert sync_scripts.check(project) is True
