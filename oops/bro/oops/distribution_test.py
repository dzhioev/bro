import configparser
import subprocess
import zipfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parents[2]
_REQUIRED_FILES = {
  'bro/oops/__init__.py',
  'bro/oops/_entrypoints.py',
  'bro/oops/assets.py',
  'bro/oops/infra/buildspec.yml',
  'bro/oops/infra/deploy_lib.sh',
  'bro/oops/infra/monitor_ecs.sh',
  'bro/oops/infra/server_base/Dockerfile',
  'bro/oops/infra/server_base/install_launcher.sh',
}


@pytest.fixture(scope='module')
def wheel(tmp_path_factory) -> Path:
  output_directory = tmp_path_factory.mktemp('bro-oops-wheel')
  subprocess.run(
    ['uv', 'build', '--wheel', str(_PROJECT), '--out-dir', str(output_directory)],
    capture_output=True,
    check=True,
  )
  return next(output_directory.glob('*.whl'))


def test_wheel_carries_deployment_assets(wheel):
  with zipfile.ZipFile(wheel) as archive:
    files = {name for name in archive.namelist() if not name.endswith('/')}
  assert _REQUIRED_FILES <= files


def test_wheel_declares_asset_directory_scripts(wheel):
  metadata = configparser.ConfigParser()
  with zipfile.ZipFile(wheel) as archive:
    path = next(name for name in archive.namelist() if name.endswith('.dist-info/entry_points.txt'))
    metadata.read_string(archive.read(path).decode())
  scripts = metadata['console_scripts']
  assert scripts['bro-oops-dir'] == 'bro.oops._entrypoints:bro_oops_assets'
  assert scripts['bro.oops.assets'] == 'bro.oops._entrypoints:bro_oops_assets'
