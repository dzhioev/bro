"""The Claude Code plugin an interactive session loads: `plugin/` beside this
module, which draws the batch each watch rewake brings into the transcript."""

import shutil
from pathlib import Path

REWAKE_RECORD_ENV = 'RIDE_REWAKE_RECORD'

_SOURCE = Path(__file__).with_name('plugin')


def provision(state_dir: Path) -> Path:
  """Lay a fresh copy of the plugin in `state_dir` and return it.

  Claude Code writes type declarations into the plugin folder an interactive
  session loads, so a session loads a copy of its own, never the installed one.
  """
  target = state_dir / 'plugin'
  if target.exists():
    shutil.rmtree(target)
  shutil.copytree(_SOURCE, target)
  return target
