import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

SETUP_SCRIPT = Path(__file__).resolve().parent / 'setup.sh'
MANIFESTS = ('pyproject.toml', 'uv.lock', 'dev/pyproject.toml', 'local/pyproject.toml')


def _stub_command(path: Path, body: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(f'#!/usr/bin/env bash\n{body}\n')
  path.chmod(0o755)


def _stub_tree(tmp_path: Path) -> Path:
  """A repository the real setup.sh can provision: stub manifests, venv and commands."""
  tree = tmp_path / 'tree'
  tree.mkdir()
  shutil.copy(SETUP_SCRIPT, tree / 'setup.sh')
  for relative_path in MANIFESTS:
    manifest = tree / relative_path
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f'{relative_path} as resolved\n')
  _stub_command(tree / '.venv' / 'bin' / 'activate', ':')
  _stub_command(tree / 'bin' / 'uv', f'echo "$@" >> "{tree}/uv-calls"')
  _stub_command(tree / 'bin' / 'bro.dev.install', ':')
  return tree


def _staged_manifests(tmp_path: Path, tree: Path) -> Path:
  """The manifest copies a baked venv was resolved from, as the entrypoint stages them."""
  staged = tmp_path / 'staged'
  for relative_path in MANIFESTS:
    copy = staged / relative_path
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(tree / relative_path, copy)
  return staged


def _provision(tree: Path, staged: Optional[Path]) -> subprocess.CompletedProcess[str]:
  environment = {
    **os.environ,
    'PATH': f'{tree / "bin"}{os.pathsep}{os.environ["PATH"]}',
  }
  environment.pop('CW_VENV_MANIFEST', None)
  if staged is not None:
    environment['CW_VENV_MANIFEST'] = str(staged)
  return subprocess.run(
    [str(tree / 'setup.sh')], capture_output=True, text=True, check=True, env=environment
  )


def _sync_calls(tree: Path) -> list[str]:
  calls = tree / 'uv-calls'
  return calls.read_text().splitlines() if calls.is_file() else []


def test_reuses_the_linked_venv_while_the_manifests_match(tmp_path):
  tree = _stub_tree(tmp_path)
  _provision(tree, _staged_manifests(tmp_path, tree))
  assert _sync_calls(tree) == []


def test_syncs_when_a_manifest_moved_away_from_the_linked_venv(tmp_path):
  tree = _stub_tree(tmp_path)
  staged = _staged_manifests(tmp_path, tree)
  (tree / 'dev' / 'pyproject.toml').write_text('a dependency bump\n')
  completed = _provision(tree, staged)
  assert [call.split()[0] for call in _sync_calls(tree)] == ['sync']
  assert 'syncing' in completed.stderr


def test_syncs_without_a_linked_venv(tmp_path):
  tree = _stub_tree(tmp_path)
  _provision(tree, None)
  assert [call.split()[0] for call in _sync_calls(tree)] == ['sync']


def test_syncs_when_no_manifest_was_staged(tmp_path):
  tree = _stub_tree(tmp_path)
  empty = tmp_path / 'empty'
  empty.mkdir()
  _provision(tree, empty)
  assert [call.split()[0] for call in _sync_calls(tree)] == ['sync']
