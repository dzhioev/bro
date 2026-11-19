import os
import shutil
import subprocess
from pathlib import Path

SETUP_SCRIPT = Path(__file__).resolve().parents[3] / 'setup.sh'


def _stub_command(path: Path, body: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(f'#!/usr/bin/env bash\n{body}\n')
  path.chmod(0o755)


def test_setup_syncs_on_every_run_without_activating_the_callers_environment(tmp_path):
  tree = tmp_path / 'tree'
  tree.mkdir()
  shutil.copy(SETUP_SCRIPT, tree / 'setup.sh')
  shutil.copytree(SETUP_SCRIPT.parent / 'bro' / 'setup', tree / 'bro' / 'setup')
  _stub_command(tree / '.venv' / 'bin' / 'activate', ':')
  _stub_command(
    tree / 'bin' / 'uv',
    f'test -z "${{VIRTUAL_ENV:-}}"\nprintf "%s\\n" "$1" >> "{tree}/uv-calls"',
  )
  _stub_command(tree / 'bin' / 'python', ':')
  _stub_command(tree / 'bin' / 'bro.dev.install', ':')
  environment = {
    **os.environ,
    'PATH': f'{tree / "bin"}{os.pathsep}{os.environ["PATH"]}',
    'VIRTUAL_ENV': '/other/environment',
  }
  for _ in range(2):
    subprocess.run([str(tree / 'setup.sh')], capture_output=True, check=True, env=environment)
  assert (tree / 'uv-calls').read_text().splitlines() == ['sync', 'sync']
