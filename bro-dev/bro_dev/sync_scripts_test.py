import subprocess
import sys


def test_imports_without_git_metadata(tmp_path):
  # importing the generator must not require an operated repo or its git metadata
  result = subprocess.run(
    [sys.executable, '-c', 'import bro_dev.sync_scripts'],
    capture_output=True,
    text=True,
    cwd=tmp_path,
  )
  assert result.returncode == 0, f'stderr: {result.stderr}'
