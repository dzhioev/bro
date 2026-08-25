"""the one graded trial the live benchmark checks drive, and the gates it needs.

Driving it spends real tokens, so the task, the narrowing of the pinned config,
what a host must have to run it, and what a finished run must hold are settled
here once rather than per check.
"""

import json
import subprocess
from pathlib import Path

import pytest
from harbor.cli.config_sources import load_config_source

from bro.base import credentials
from bro.base.suite_environment import host_credential_store, token_spending_skip_reason
from bro.benchmark.bundle import host_mismatch

# the smallest image in the set, and one carrying neither python3 nor a CA
# store — so a single trial exercises the bundle and SSL_CERT_FILE for real
TASK = 'terminal-bench/adaptive-rejection-sampler'
JOB_CONFIG = Path(__file__).with_name('terminal_bench_2_1.yaml')

# the credentials the trials will actually hydrate — the config names them
_LLM_CREDENTIALS = sorted(
  {agent['kwargs']['llm_credential'] for agent in load_config_source(JOB_CONFIG)['agents']}
)


def _available(*command: str) -> bool:
  try:
    return subprocess.run(command, capture_output=True).returncode == 0
  except FileNotFoundError:
    return False


def _host_holds_the_llm_keys() -> bool:
  with host_credential_store():
    return all(credentials.default_store().available_instance(name) for name in _LLM_CREDENTIALS)


_HOST_MISMATCH = host_mismatch()
_TOKENS_WITHHELD = token_spending_skip_reason()

LIVE_TRIAL = [
  pytest.mark.skipif(_TOKENS_WITHHELD is not None, reason=_TOKENS_WITHHELD or ''),
  pytest.mark.skipif(not _available('docker', 'info'), reason='no reachable docker daemon'),
  pytest.mark.skipif(_HOST_MISMATCH is not None, reason=str(_HOST_MISMATCH)),
  # harbor drives every container through the compose CLI plugin, which is
  # installed separately from the engine
  pytest.mark.skipif(
    not _available('docker', 'compose', 'version'), reason='no docker compose plugin'
  ),
  pytest.mark.skipif(
    not _host_holds_the_llm_keys(), reason='an LLM key the job config names does not resolve'
  ),
]


def one_task_config(directory: Path) -> Path:
  """the pinned config narrowed to the one task, so the pins stay in one file."""
  config = load_config_source(JOB_CONFIG)
  for dataset in config['datasets']:
    dataset['task_names'] = [TASK]
  narrowed = directory / 'one-task.json'
  narrowed.write_text(json.dumps(config))
  return narrowed


def assert_graded_run(jobs: Path) -> None:
  """what the job directory of a finished run must hold, whatever drove it."""
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
  job = results[0].parent
  assert len(list(job.rglob('agent/bro.log'))) > 0, 'the trial kept no activity log'
  # the local store's own layout under the run's data home, found rather than spelled
  assert len(list(job.rglob('agent/ride/trails/*/*/header.json'))) > 0, 'the trial kept no trail'
