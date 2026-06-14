"""durable health state for session-log sync.

`sync-session-log` writes this file after every attempt; `session-log-statusline`
and `cw banner` read it (no network) to warn when sync is failing. Without it a
broken sync is silent — the watcher logs to stderr that the hooks throw away.

Stdlib-only on purpose: the statusline imports this on every render, so it must
not pull in boto3 (as importing `sync_session_log` would).
"""

import datetime
import json
from pathlib import Path

HEALTH_PATH = Path.home() / '.claude' / 'session-log-sync-health.json'

# cap the stored error so a verbose boto traceback can't bloat the file
_MAX_ERROR = 500


def write(status: str, error: str | None = None) -> None:
  """atomically record the latest sync outcome. never raises — health
  reporting must not be able to break the sync it reports on."""
  payload = {
    'status': status,
    'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'error': error[:_MAX_ERROR] if error is not None else None,
  }
  try:
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEALTH_PATH.with_name(HEALTH_PATH.name + '.tmp')
    tmp.write_text(json.dumps(payload))
    tmp.replace(HEALTH_PATH)
  except OSError:
    pass


def is_failing() -> bool:
  """True when the last recorded sync attempt failed; absent/unreadable → False."""
  try:
    data = json.loads(HEALTH_PATH.read_text())
  except (OSError, json.JSONDecodeError):
    return False
  return isinstance(data, dict) and data.get('status') == 'error'
