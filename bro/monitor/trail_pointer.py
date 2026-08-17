"""a managed session's current-trail pointer.

A Claude recorder writes the pointer in its session config directory. A native
bro root is published by the supervising broker into the workspace record
directory. Both placements are host-readable and let resume and summon
provenance identify the session trail without querying the trails service. An
absent pointer means the session published no trail.

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


def write(target: Path, trail_id: str) -> None:
  """atomically point `target` at `trail_id` without breaking its publisher."""
  try:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.tmp')
    tmp.write_text(json.dumps({'trail_id': trail_id}))
    tmp.replace(target)
  except OSError:
    pass


def publish(trail_id: str) -> None:
  """publish the current recorder trail at the process-default pointer."""
  write(path(), trail_id)


def clear(target: Optional[Path] = None) -> None:
  """drop a pointer — no trail is being recorded. never raises."""
  try:
    (path() if target is None else target).unlink(missing_ok=True)
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
