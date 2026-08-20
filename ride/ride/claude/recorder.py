"""the session recorder daemon the in-place runner starts next to claude.

Every session flavor gets continuous transcript recording to trails from one
mechanism: the runner spawns the recorder before launching claude and stops it
after claude exits — the stop is the daemon's final append and trail end
(`ride/ride/claude/trail_recorder.py` owns transcript acquisition). Deliberately
not a Claude Code hook: `--raw` sessions run `claude --bare`, which runs no
hooks at all.

The daemon's stderr goes to the session state dir, among the claude harness's
own session artifacts; its durable failure signal is the health file
(`monitor/health.py`) surfaced by the status line and `ride banner`. That signal
reports a daemon that ran and stopped reporting — one that cannot be started at
all ends the launch instead, since a session that believes it is recording and
is not is worse than one that refuses to start.
"""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bro.base import log, spawn
from bro.monitor import SESSION_DIR_ENV, claude_projects_dir, harness_session_dir

RECORDER_COMMAND = 'ride.claude.trail-recorder'

# a stop bounds the final snapshot + end — one transcript upload, seconds even
# for a large session
_STOP_TIMEOUT = 60.0


@dataclass
class _SessionRecorder:
  """a session-owned recorder daemon."""

  process: subprocess.Popen
  log_path: Path

  def stop(self) -> None:
    self.process.terminate()
    try:
      self.process.wait(timeout=_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
      log.warning(
        'session recorder did not finish within %.0fs; killing (log: %s)',
        _STOP_TIMEOUT,
        self.log_path,
      )
      self.process.kill()
      self.process.wait()


def start_session_recorder(
  name: str,
  workspace: Path,
  env: Mapping[str, str],
  llm: dict,
) -> _SessionRecorder:
  """start the recorder daemon over this session's transcripts. `llm` is the
  launch recipe stored as the trail's `native.llm`. raises RuntimeError when the
  daemon cannot start."""
  projects_dir = claude_projects_dir(workspace)
  session_state = harness_session_dir('claude')
  if session_state is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset: this session keeps no state dir')
  log_path = session_state / 'session-recorder.log'
  try:
    argv = [
      spawn.console_script(RECORDER_COMMAND),
      '--workspace',
      name,
      '--projects-dir',
      str(projects_dir),
      '--llm',
      json.dumps(llm),
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'a') as log_file:
      process = spawn.popen(argv, env=dict(env), stdout=log_file, stderr=subprocess.STDOUT)
  except OSError as error:
    raise RuntimeError(f'cannot start the session recorder: {error}') from error
  log.verbose('session recorder started (log: %s)', log_path)
  return _SessionRecorder(process, log_path)
