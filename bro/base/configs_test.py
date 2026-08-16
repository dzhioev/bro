import importlib.metadata
import importlib.util
import os
import subprocess
import sys

from bro.base import configs


def test_version_matches_the_frameworks_own_distribution():
  assert configs.VERSION == importlib.metadata.version(configs.DISTRIBUTION)


def test_the_package_carrying_the_version_is_owned_by_no_one_distribution():
  package = configs.__name__.partition('.')[0]
  spec = importlib.util.find_spec(package)
  assert spec is not None and spec.origin is None, f'{package} is a regular package now'


def test_bro_configs_dir_reads_the_environment(tmp_path):
  value = str(tmp_path / 'service-configs')
  result = subprocess.run(
    [sys.executable, '-c', 'from bro.base import configs; print(configs.BRO_CONFIGS_DIR)'],
    check=True,
    capture_output=True,
    text=True,
    env={**os.environ, 'BRO_CONFIGS_DIR': value},
  )
  assert result.stdout.strip() == value


def test_empty_bro_configs_dir_fails_fast():
  result = subprocess.run(
    [sys.executable, '-c', 'from bro.base import configs'],
    capture_output=True,
    text=True,
    env={**os.environ, 'BRO_CONFIGS_DIR': ''},
  )
  assert result.returncode != 0
  assert 'BRO_CONFIGS_DIR must not be empty' in result.stderr
