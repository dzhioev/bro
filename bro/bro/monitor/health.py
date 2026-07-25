"""durable health state for session recording.

`session-log.recorder` writes this file after every attempt;
`session-log.statusline` and `cw banner` read it (no network) to warn when
recording is failing. Without it a broken recorder is silent — the daemon's
stderr goes to a per-session log file (`<claude config dir>/session-recorder.log`)
nobody watches live.

The file lives under the session's claude config dir (`CLAUDE_CONFIG_DIR` when
set — every cw session points it at private per-session state), so concurrent
sessions don't clobber each other's signal.

Stdlib-only on purpose: the statusline imports this on every render, so it must
stay dependency-free.
"""

import datetime
import json
import os
from pathlib import Path
from typing import Optional

# cap the stored error so a verbose boto traceback can't bloat the file
_MAX_ERROR = 500


def health_path() -> Path:
  config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
  root = Path(config_dir) if config_dir is not None else Path.home() / '.claude'
  return root / 'session-recorder-health.json'


def write(status: str, error: Optional[str] = None) -> None:
  """atomically record the latest recording outcome. never raises — health
  reporting must not be able to break the recording it reports on."""
  payload = {
    'status': status,
    'checked_at': datetime.datetime.now(datetime.UTC).isoformat(),
    'error': error[:_MAX_ERROR] if error is not None else None,
  }
  try:
    path = health_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
  except OSError:
    pass


def is_failing() -> bool:
  """True when the last recording attempt failed; absent/unreadable → False."""
  try:
    data = json.loads(health_path().read_text())
  except (OSError, json.JSONDecodeError):
    return False
  return isinstance(data, dict) and data.get('status') == 'error'
