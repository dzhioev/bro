"""the session-log sync daemon the in-place runner starts next to claude.

Every session flavor gets continuous transcript upload from one mechanism: the
runner spawns `sync-session-log --watch` before launching claude and stops it
after claude exits — the stop is the daemon's final sync (leave event + upload;
sync_session_log.py owns the conversation model). Deliberately not a Claude
Code hook: `--bro` sessions run `claude --bare`, which runs no hooks at all.

The daemon's stderr goes to `<claude config dir>/session-log-sync.log`; its
durable failure signal is the health file (session_log_health.py) surfaced by
the statusLine and `cw banner`.
"""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import log, spawn
from cw.paths import _claude_config_dir, _claude_projects_dir

# a stop bounds the final sync — one transcript upload, seconds even for a
# large session
_STOP_TIMEOUT = 60.0


@dataclass
class _SessionLogSync:
  """a session-owned `sync-session-log --watch` daemon."""

  process: subprocess.Popen
  log_path: Path

  def stop(self) -> None:
    self.process.terminate()
    try:
      self.process.wait(timeout=_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
      log.warning(
        'session-log sync did not finish within %.0fs; killing (log: %s)',
        _STOP_TIMEOUT,
        self.log_path,
      )
      self.process.kill()
      self.process.wait()


def _start_session_log_sync(
  name: str,
  workspace: Path,
  env: Mapping[str, str],
  resume_segment: Optional[str] = None,
) -> Optional[_SessionLogSync]:
  """start the sync daemon over this session's transcripts. returns None — with
  a warning — when the daemon cannot start; the session then stays invisible to
  `sessions` / `rewind` but the launch proceeds."""
  projects_dir = _claude_projects_dir(workspace)
  log_path = _claude_config_dir() / 'session-log-sync.log'
  argv = [
    'sync-session-log',
    '--watch',
    '--workspace',
    name,
    '--projects-dir',
    str(projects_dir),
  ]
  if resume_segment is not None:
    argv.extend(['--resume-segment', resume_segment])
  try:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'a') as log_file:
      process = spawn.popen(argv, env=dict(env), stdout=log_file, stderr=subprocess.STDOUT)
  except OSError as e:
    log.warning('cannot start session-log sync (%s); the session is not uploaded', e)
    return None
  log.verbose('session-log sync started (log: %s)', log_path)
  return _SessionLogSync(process, log_path)
