import subprocess
import zipfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parents[1]
_SHIPPED_SETUP_FILES = {
  'ride/setup/base_image/Dockerfile',
  'ride/setup/base_image/build.sh',
  'ride/setup/container/Dockerfile',
  'ride/setup/container/claude-code-version',
  'ride/setup/container/entrypoint.sh',
  'ride/setup/container/git.sh',
}
_NOT_SHIPPED = {
  'ride/setup/container/bump-claude-code.sh',
  'ride/setup/container/test_smoke.sh',
}


@pytest.fixture(scope='module')
def wheel_files(tmp_path_factory) -> set[str]:
  output_directory = tmp_path_factory.mktemp('ride-wheel')
  subprocess.run(
    ['uv', 'build', '--wheel', str(_PROJECT), '--out-dir', str(output_directory)],
    capture_output=True,
    check=True,
  )
  with zipfile.ZipFile(next(output_directory.glob('*.whl'))) as archive:
    return {name for name in archive.namelist() if not name.endswith('/')}


def test_container_assets_ship_in_bro_ride(wheel_files):
  assert _SHIPPED_SETUP_FILES <= wheel_files


def test_checkout_only_container_scripts_do_not_ship(wheel_files):
  assert _NOT_SHIPPED.isdisjoint(wheel_files)
