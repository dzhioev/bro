import functools
from collections.abc import Callable
from typing import Optional

from bro.base import log
from bro.workspace.paths import RuntimeLocationError, WorkspaceNameError
from ride.runtime_state import RuntimeStateMigrationError

_Main = Callable[[list[str]], Optional[int]]


def reports_runtime_errors(main: _Main) -> _Main:
  """Render runtime-location, workspace-name, and state-migration failures as CLI errors."""

  @functools.wraps(main)
  def wrapper(argv: list[str]) -> Optional[int]:
    try:
      return main(argv)
    except (RuntimeLocationError, WorkspaceNameError, RuntimeStateMigrationError) as error:
      log.error('%s', error)
      return 1

  return wrapper
