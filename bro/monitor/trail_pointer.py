"""a managed session's current-trail pointer.

The recording side publishes the pointer into the session's own state dir, which
is also a workspace record (`session_pointer`) — so resume and summon provenance
identify the session trail host-side without querying the trails service. An
absent pointer means the session published no trail.

Stdlib-only on purpose: the host-side reader must not pull in service
dependencies.
"""

import json
from pathlib import Path
from typing import Optional

from bro.monitor import session_dir, workspace_session_dir

FILENAME = 'current-trail.json'


def path() -> Optional[Path]:
  """the pointer of the session this process runs in, or None outside one."""
  session = session_dir()
  return session / FILENAME if session is not None else None


def session_pointer(workspace: Path) -> Path:
  """a workspace's pointer among its session records — the host-side name for
  the file `path` reaches from inside the session."""
  return workspace_session_dir(workspace) / FILENAME


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
  target = path()
  if target is not None:
    write(target, trail_id)


def clear(target: Optional[Path] = None) -> None:
  """drop a pointer — no trail is being recorded. never raises."""
  pointer = path() if target is None else target
  if pointer is None:
    return
  try:
    pointer.unlink(missing_ok=True)
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
