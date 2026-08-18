import subprocess

import pytest
import yaml

from bro.local import run_tests


def test_benchmark_stage_refuses_to_rewrite_a_stale_lock(monkeypatch, tmp_path):
  directory = tmp_path / run_tests.BENCHMARK
  directory.mkdir()
  project_file = directory / 'pyproject.toml'
  project_file.write_text(
    """
[project]
name = "lock-probe"
version = "0.1.0"
requires-python = ">=3.12"

[tool.uv]
package = false
""".lstrip()
  )
  subprocess.run(('uv', 'lock'), check=True, cwd=directory, capture_output=True)
  lock_file = directory / 'uv.lock'
  locked = lock_file.read_bytes()
  project_file.write_text(project_file.read_text().replace('0.1.0', '0.2.0'))
  monkeypatch.setattr(run_tests, 'DIR', tmp_path)

  with pytest.raises(subprocess.CalledProcessError) as raised:
    run_tests.benchmark_stage()

  assert raised.value.cmd == ('uv', 'sync', '--locked', '--all-groups')
  assert lock_file.read_bytes() == locked


def test_the_workflow_matrix_names_every_gate_stage():
  workflow = yaml.safe_load((run_tests.DIR / '.github/workflows/tests.yml').read_text())

  matrix = workflow['jobs']['stage']['strategy']['matrix']['stage']

  assert matrix == [stage.name for stage in run_tests.STAGES]
