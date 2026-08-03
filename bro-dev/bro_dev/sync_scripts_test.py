import subprocess
import sys


def test_imports_without_git_metadata(tmp_path):
  # the image build runs `python -m dev.sync_scripts --entrypoints` over a tree
  # staged without .git
  result = subprocess.run(
    [sys.executable, '-c', 'import dev.sync_scripts'],
    capture_output=True,
    text=True,
    cwd=tmp_path,
  )
  assert result.returncode == 0, f'stderr: {result.stderr}'
