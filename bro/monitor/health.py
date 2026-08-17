"""durable health state for session recording.

The Claude recorder beats this file on every attempt; `ride.claude.statusline` and
`cw banner` read it without a network call to warn when recording is failing or
has stopped. Without it a broken recorder is silent — the daemon's stderr goes
to a per-session log file (`<claude config dir>/session-recorder.log`) nobody
watches live, and a daemon killed by a signal writes nothing at all, so a
missed beat is the only trace it leaves.

The file lives under the session's claude config dir (`CLAUDE_CONFIG_DIR` when
set — every cw session points it at private per-session state), so concurrent
sessions don't clobber each other's signal.

Stdlib-only on purpose: the statusline imports this on every render, so it must
stay dependency-free.
"""

import datetime
import json
from pathlib import Path
from typing import Optional

from bro.monitor import claude_config_dir

# cap the stored error so a verbose boto traceback can't bloat the file
_MAX_ERROR = 500

# how long past its next expected beat a writer may run before it counts as
# gone. Generous: a single attempt can sit in a slow store round-trip, and
# calling a live recorder dead is the worse mistake
_BEAT_GRACE = 60.0

_FAILING = 'FAILING — see session-recorder.log'
_STOPPED = 'STOPPED — the recorder is no longer running; see session-recorder.log'


def health_path() -> Path:
  return claude_config_dir() / 'session-recorder-health.json'


def write(status: str, error: Optional[str] = None, *, interval: Optional[float]) -> None:
  """atomically beat the file with the latest recording outcome. `interval` is
  the writer's polling period, stamped into the file as the age at which readers
  must treat the writer as gone; pass None for a final write no beat follows.
  never raises — health reporting must not be able to break the recording it
  reports on."""
  payload = {
    'status': status,
    'checked_at': datetime.datetime.now(datetime.UTC).isoformat(),
    'stale_after': interval + _BEAT_GRACE if interval is not None else None,
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


def _read() -> Optional[dict]:
  try:
    data = json.loads(health_path().read_text())
  except (OSError, json.JSONDecodeError):
    return None
  return data if isinstance(data, dict) else None


def _writer_is_gone(data: dict) -> bool:
  """True when the beat the writer promised is overdue. A payload promising no
  further beat never is."""
  stale_after = data.get('stale_after')
  if not isinstance(stale_after, (int, float)):
    return False
  try:
    beat = datetime.datetime.fromisoformat(data['checked_at'])
    age = (datetime.datetime.now(datetime.UTC) - beat).total_seconds()
  except (KeyError, TypeError, ValueError):
    return False
  return age > stale_after


def problem() -> Optional[str]:
  """the recording trouble to warn about; None when recording is healthy, and
  when nothing reports on it at all. A recorded failure outranks a missed beat:
  it names the cause."""
  data = _read()
  if data is None:
    return None
  if data.get('status') == 'error':
    return _FAILING
  return _STOPPED if _writer_is_gone(data) else None
