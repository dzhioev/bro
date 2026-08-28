import configparser
import subprocess
import zipfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parents[2]
_DEVELOPMENT_DOMAIN = {
  'bro/extra/github/poll_pr.py',
  'bro/prompts/dev/style.md',
  'bro/workflow/commit_footer.py',
  'bro/workflow/hooks/commit-msg',
  'bro/workflow/hooks/post-commit',
  'bros/dev/__init__.py',
  'bros/dev/spells/fix.md',
  'bros/eyebro/__init__.py',
  'bros/eyebro/spells/review-diff.md',
  'bros/lead/__init__.py',
  'bros/lead/spells/run-feature.md',
  'bros/terminal/__init__.py',
}
_CORE_GITHUB = {
  'bro/extra/github/api.py',
  'bro/extra/github/app.py',
}


@pytest.fixture(scope='module')
def wheel(tmp_path_factory) -> Path:
  output_directory = tmp_path_factory.mktemp('bro-dev-wheel')
  subprocess.run(
    ['uv', 'build', '--wheel', str(_PROJECT), '--out-dir', str(output_directory)],
    capture_output=True,
    check=True,
  )
  return next(output_directory.glob('*.whl'))


@pytest.fixture(scope='module')
def wheel_files(wheel) -> set[str]:
  with zipfile.ZipFile(wheel) as archive:
    return {name for name in archive.namelist() if not name.endswith('/')}


@pytest.fixture(scope='module')
def entry_points(wheel) -> configparser.ConfigParser:
  metadata = configparser.ConfigParser()
  with zipfile.ZipFile(wheel) as archive:
    path = next(name for name in archive.namelist() if name.endswith('.dist-info/entry_points.txt'))
    metadata.read_string(archive.read(path).decode())
  return metadata


def test_development_domain_ships_in_bro_dev(wheel_files):
  assert _DEVELOPMENT_DOMAIN <= wheel_files


def test_core_github_client_does_not_move_with_poll_pr(wheel_files):
  assert _CORE_GITHUB.isdisjoint(wheel_files)


def test_development_personas_are_declared_by_bro_dev(entry_points):
  assert set(entry_points['bro']) == {'analyst', 'dev', 'eyebro', 'lead', 'terminal'}


def test_development_scripts_are_declared_by_bro_dev(entry_points):
  assert {'commit-footer', 'fold-branch', 'land-pr', 'poll-pr'} <= set(
    entry_points['console_scripts']
  )
