"""the packaging policy: no distribution a repository builds ships a test module."""

import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

TEST_MODULE_NAMES = ('conftest',)
TEST_MODULE_SUFFIXES = ('_test', '_test_helper')


def is_test_module(path: str) -> bool:
  if not path.endswith('.py'):
    return False
  module = path.rsplit('/', 1)[-1].removesuffix('.py')
  return module in TEST_MODULE_NAMES or module.endswith(TEST_MODULE_SUFFIXES)


def shipped_test_modules(wheel: Path) -> list[str]:
  """the test modules a built wheel carries."""
  with zipfile.ZipFile(wheel) as archive:
    return sorted(name for name in archive.namelist() if is_test_module(name))


def distribution_roots(repo_root: Path) -> list[Path]:
  """every directory the repository builds a distribution from."""
  metadata = tomllib.loads((repo_root / 'pyproject.toml').read_text())
  members = metadata.get('tool', {}).get('uv', {}).get('workspace', {}).get('members', [])
  return [repo_root] + sorted(
    path for pattern in members for path in repo_root.glob(pattern) if path.is_dir()
  )


def build_wheels(repo_root: Path, output_directory: Path) -> list[Path]:
  for directory in distribution_roots(repo_root):
    command = ['uv', 'build', str(directory), '--out-dir', str(output_directory)]
    result = subprocess.run(command, cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
      raise AssertionError(f'`{" ".join(command)}` failed:\n{result.stderr}')
  return sorted(output_directory.glob('*.whl'))


def assert_packaging_policy(repo_root: Path) -> None:
  with tempfile.TemporaryDirectory() as directory:
    wheels = build_wheels(repo_root, Path(directory))
    if len(wheels) == 0:
      raise AssertionError(f'no wheel was built from {repo_root}')
    problems = [
      f'{wheel.name} ships {module}' for wheel in wheels for module in shipped_test_modules(wheel)
    ]
  if len(problems) > 0:
    raise AssertionError('\n'.join(problems))
