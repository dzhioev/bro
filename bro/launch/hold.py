"""the session hold — its user-involvement level — as the environment carries it."""

import os
from typing import Optional

HOLD_VARIABLE = 'BRO_HOLD'
UNATTENDED = 'unattended'


def session_hold() -> Optional[str]:
  """the hold of the session this process runs in, None outside a managed one."""
  return os.environ.get(HOLD_VARIABLE)


def interactive_session() -> bool:
  """whether a human channel exists — every hold but unattended, and nothing
  outside a managed session."""
  hold = session_hold()
  return hold is not None and hold != UNATTENDED
