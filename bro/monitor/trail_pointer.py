"""the session's current-trail pointer.

The recorder daemon publishes the trail id it is currently recording, so the
trail a live session lands in is readable without a query to the trails service.
The file lives under the session's claude config dir — readable from inside the
session and, in both session modes, from the host (a container session's dir is
the mounted `~/.claude/cw-sessions/<name>`), the same placement as the
recording-health file; `cw` derives that host-side path and threads it to the
broker root. The pointer is absent until the recorder adopts a transcript, and
stays absent for a session nothing records: no trail published is an answer
readers carry, not a state they wait out.

Stdlib-only on purpose: the host-side reader must not pull in service
dependencies.
"""

import json
from pathlib import Path
from typing import Optional

from bro.monitor import claude_config_dir

FILENAME = 'current-trail.json'


def path() -> Path:
  return claude_config_dir() / FILENAME


def publish(trail_id: str) -> None:
  """atomically point the file at `trail_id`. never raises — publishing must
  not be able to break the recording it points at."""
  try:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.tmp')
    tmp.write_text(json.dumps({'trail_id': trail_id}))
    tmp.replace(target)
  except OSError:
    pass


def clear() -> None:
  """drop the pointer — no trail is being recorded. never raises."""
  try:
    path().unlink(missing_ok=True)
  except OSError:
    pass


def read(pointer: Path) -> Optional[str]:
  """the published trail id, or None when the file is absent or unreadable."""
  try:
    data = json.loads(pointer.read_text())
  except (OSError, json.JSONDecodeError):
    return None
  trail_id = data.get('trail_id') if isinstance(data, dict) else None
  return trail_id if isinstance(trail_id, str) and len(trail_id) > 0 else None
