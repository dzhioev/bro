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

import subprocess
import sys
from pathlib import Path

from bro.benchmark.bundle import build, default_root, workspace_root
from bro.benchmark.e2e_test_helper import LIVE_TRIAL, assert_graded_run, one_task_config

HARBOR = Path(sys.executable).with_name('harbor')

pytestmark = LIVE_TRIAL


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
      str(one_task_config(tmp_path)),
      '--jobs-dir',
      str(jobs),
      '--yes',
      '--quiet',
    ],
    check=True,
  )

  assert_graded_run(jobs)
