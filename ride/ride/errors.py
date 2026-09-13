import functools
from collections.abc import Callable
from typing import Optional

from bro.base import log
from bro.workspace.paths import RuntimeLocationError, WorkspaceNameError
from ride.workspace.metadata import UnrecognizedWorkspaceRecord

_Main = Callable[[list[str]], Optional[int]]


def reports_runtime_errors(main: _Main) -> _Main:
  """Render runtime-location and workspace-record failures as CLI errors."""

  @functools.wraps(main)
  def wrapper(argv: list[str]) -> Optional[int]:
    try:
      return main(argv)
    except (RuntimeLocationError, WorkspaceNameError, UnrecognizedWorkspaceRecord) as error:
      log.error('%s', error)
      return 1

  return wrapper
