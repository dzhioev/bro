"""live check of one real Terminal-Bench task driven through harbor end to end.

Passing the task is not the point and is not expected — what this asserts is
that every seam holds: the bundle installs into a foreign image, the credential
store resolves there, the bro drives the LLM loop against the task filesystem,
and the verifier grades what it left behind. It runs the documented commands
rather than the library, so what is checked is the path an operator uses.

It builds a real bundle, drives the host docker daemon and spends real tokens,
so it stays out of the gate's roster:

  uv run --directory benchmark pytest bro/benchmark/harbor_e2e_test.py
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from harbor.cli.config_sources import load_config_source

from bro.base import credentials
from bro.benchmark.bundle import build, default_root, host_mismatch, workspace_root

# the smallest image in the set, and one carrying neither python3 nor a CA
# store — so a single trial exercises the bundle and SSL_CERT_FILE for real
TASK = 'terminal-bench/adaptive-rejection-sampler'
JOB_CONFIG = Path(__file__).with_name('terminal_bench_2_1.yaml')
HARBOR = Path(sys.executable).with_name('harbor')

# the credentials the trials will actually hydrate — the config names them
_LLM_CREDENTIALS = sorted(
  {agent['kwargs']['llm_credential'] for agent in load_config_source(JOB_CONFIG)['agents']}
)


def _available(*command: str) -> bool:
  try:
    return subprocess.run(command, capture_output=True).returncode == 0
  except FileNotFoundError:
    return False


_HOST_MISMATCH = host_mismatch()

pytestmark = [
  pytest.mark.skipif(not _available('docker', 'info'), reason='no reachable docker daemon'),
  pytest.mark.skipif(_HOST_MISMATCH is not None, reason=str(_HOST_MISMATCH)),
  # harbor drives every container through the compose CLI plugin, which is
  # installed separately from the engine
  pytest.mark.skipif(
    not _available('docker', 'compose', 'version'), reason='no docker compose plugin'
  ),
  pytest.mark.skipif(
    not all(credentials.default_store().available_instance(name) for name in _LLM_CREDENTIALS),
    reason='an LLM key the job config names does not resolve',
  ),
]


def _one_task_config(directory: Path) -> Path:
  """the pinned config narrowed to the one task, so the pins stay in one file."""
  config = load_config_source(JOB_CONFIG)
  for dataset in config['datasets']:
    dataset['task_names'] = [TASK]
  narrowed = directory / 'one-task.json'
  narrowed.write_text(json.dumps(config))
  return narrowed


def test_a_real_task_is_driven_and_graded(tmp_path):
  workspace = workspace_root()
  build(workspace, default_root(workspace))
  jobs = tmp_path / 'jobs'

  subprocess.run(
    [
      str(HARBOR),
      'job',
      'start',
      '--config',
      str(_one_task_config(tmp_path)),
      '--jobs-dir',
      str(jobs),
      '--yes',
      '--quiet',
    ],
    check=True,
  )

  results = sorted(jobs.glob('*/result.json'))
  assert len(results) == 1
  stats = json.loads(results[0].read_text())['stats']
  for name, evaluated in stats['evals'].items():
    assert evaluated['n_trials'] > 0, f'{name} produced no graded trial'
    assert evaluated['reward_stats'] != {}, f'{name} produced no reward'
  # what the reward alone cannot tell: a bro that died on startup is graded zero
  # like one that worked the task and failed. Tokens mean the loop actually ran,
  # and that the usage file made it back out of the container
  assert stats['n_output_tokens'] > 0
