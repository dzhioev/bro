"""the session recorder daemon the in-place runner starts next to claude.

Every session flavor gets continuous transcript recording to trails from one
mechanism: the runner spawns `session-log.recorder` before launching claude and
stops it after claude exits — the stop is the daemon's final snapshot and trail
end (session_log/recorder.py owns the trail model). Deliberately not a Claude
Code hook: `--raw` sessions run `claude --bare`, which runs no hooks at all.

The daemon's stderr goes to `<claude config dir>/session-recorder.log`; its
durable failure signal is the health file (session_log/health.py) surfaced by
the statusLine and `cw banner`.
"""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import log, spawn
from cw.claude_config import _claude_config_dir, _claude_projects_dir

# a stop bounds the final snapshot + end — one transcript upload, seconds even
# for a large session
_STOP_TIMEOUT = 60.0


@dataclass
class _SessionRecorder:
  """a session-owned `session-log.recorder` daemon."""

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


def _start_session_recorder(
  name: str,
  workspace: Path,
  env: Mapping[str, str],
  llm: dict,
) -> Optional[_SessionRecorder]:
  """start the recorder daemon over this session's transcripts. `llm` is the
  launch recipe stored as the trail's `native.llm`. returns None — with a
  warning — when the daemon cannot start; the session then stays invisible to
  `rewind` but the launch proceeds."""
  projects_dir = _claude_projects_dir(workspace)
  log_path = _claude_config_dir() / 'session-recorder.log'
  argv = [
    'session-log.recorder',
    '--workspace',
    name,
    '--projects-dir',
    str(projects_dir),
    '--llm',
    json.dumps(llm),
  ]
  try:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'a') as log_file:
      process = spawn.popen(argv, env=dict(env), stdout=log_file, stderr=subprocess.STDOUT)
  except OSError as e:
    log.warning('cannot start the session recorder (%s); the session is not recorded', e)
    return None
  log.verbose('session recorder started (log: %s)', log_path)
  return _SessionRecorder(process, log_path)
