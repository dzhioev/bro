import zipfile
from pathlib import Path

import pytest

from bro.dev.packaging_policy import (
  assert_packaging_policy,
  distribution_roots,
  shipped_test_modules,
)

_PROJECT = """
[build-system]
requires = ["uv_build>=0.12,<0.13"]
build-backend = "uv_build"

[project]
name = "thing"
version = "1.0"

[tool.uv.build-backend]
module-root = ""
"""


def _wheel(path: Path, *members: str) -> Path:
  with zipfile.ZipFile(path, 'w') as archive:
    for member in members:
      archive.writestr(member, '')
  return path


def test_finds_every_test_module_convention(tmp_path):
  wheel = _wheel(
    tmp_path / 'thing-1.0-py3-none-any.whl',
    'thing/api.py',
    'thing/api_test.py',
    'thing/conftest.py',
    'thing/liveness_test_helper.py',
    'thing/run_tests.py',
    'thing/data/api_test.json',
  )

  assert shipped_test_modules(wheel) == [
    'thing/api_test.py',
    'thing/conftest.py',
    'thing/liveness_test_helper.py',
  ]


def test_distribution_roots_expand_the_workspace_members(tmp_path):
  (tmp_path / 'pyproject.toml').write_text('[tool.uv.workspace]\nmembers = ["packages/*"]\n')
  (tmp_path / 'packages' / 'one').mkdir(parents=True)
  (tmp_path / 'packages' / 'two').mkdir()

  assert distribution_roots(tmp_path) == [
    tmp_path,
    tmp_path / 'packages' / 'one',
    tmp_path / 'packages' / 'two',
  ]


def test_distribution_roots_take_projects_outside_the_workspace(tmp_path):
  (tmp_path / 'pyproject.toml').write_text('[tool.uv.workspace]\nmembers = ["packages/*"]\n')
  (tmp_path / 'packages' / 'one').mkdir(parents=True)
  (tmp_path / 'beside').mkdir()

  assert distribution_roots(tmp_path, siblings=('beside',)) == [
    tmp_path,
    tmp_path / 'beside',
    tmp_path / 'packages' / 'one',
  ]


def test_reports_a_distribution_built_without_the_exclusion(tmp_path):
  (tmp_path / 'pyproject.toml').write_text(_PROJECT)
  (tmp_path / 'thing').mkdir()
  (tmp_path / 'thing' / '__init__.py').write_text('')
  (tmp_path / 'thing' / 'api_test.py').write_text('')

  with pytest.raises(AssertionError, match='ships thing/api_test.py'):
    assert_packaging_policy(tmp_path)
