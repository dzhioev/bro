import json
import subprocess
import sys

from bro.local.file_bench import ROOT


def test_benchmark_command_runs_fidelity_offline():
  result = subprocess.run(
    [
      sys.executable,
      '-m',
      'bro.local.file_bench',
      '--offline',
      '--cell',
      'fidelity',
    ],
    cwd=ROOT,
    capture_output=True,
    text=True,
    timeout=180,
  )
  assert result.returncode == 0, result.stdout + result.stderr
  records = [json.loads(line) for line in result.stdout.splitlines()]
  assert any(record['cell'] == 'fidelity' for record in records)
  assert all(record['status'] != 'failed' for record in records)
  assert 'Logging error' not in result.stderr
