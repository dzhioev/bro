"""Claude Code statusLine: session warnings and live summon state.

Prints nothing when there is nothing to say, so Claude keeps its default status
bar. Three sections, joined into the one line Claude renders:

- the detached-session state when no repository is attached;
- a red `⚠ session recording …` warning when the health file reports a failing or
  a stopped recorder — the one coloured channel that survives Claude's
  alternate-screen buffer;
- the session's summons, read from the status file `RIDE_SUMMON_STATUS` points at
  (written host-side by `ride/ride/summon_control.py`): each active summon as target, trail id,
  and age, plus the last terminal outcome for a while after it lands. External
  stderr is invisible under the fullscreen TUI, so this is the session's one live
  view of an in-flight summon.

Claude re-runs this in a fresh interpreter on every render, so its whole import
closure is a standing cost and must stay small.
"""

import os
import sys
import time
from typing import Optional

from bro import summon_status
from bro.monitor import health

# how long the last terminal outcome stays on the status line; after that the
# default status bar comes back
_LAST_OUTCOME_TTL = 900.0

_RED = '\033[1;31m'
_YELLOW = '\033[1;33m'
_GREEN = '\033[1;32m'
_DIM = '\033[2m'
_RESET = '\033[0m'


def _age(seconds: float) -> str:
  seconds = max(0, int(seconds))
  if seconds < 60:
    return f'{seconds}s'
  if seconds < 3600:
    return f'{seconds // 60}m'
  return f'{seconds // 3600}h{(seconds % 3600) // 60:02d}m'


def _trail(trail_id: Optional[str]) -> str:
  if trail_id is None:
    return 'no trail yet'
  return f'trail {trail_id}'


def _summon_parts(now: float) -> list[str]:
  path = summon_status.status_path()
  if path is None:
    return []
  try:
    status = summon_status.read(path)
  except (OSError, ValueError):
    return [f'{_RED}⚠ summon status unreadable{_RESET}']
  parts = []
  for active in status.active:
    age = _age(now - active.started_at)
    if active.manual and active.trail_id is None:
      # registered but not launched: the user holds the token
      parts.append(f'{_YELLOW}⚡ awaiting manual {active.target} launch {age}{_RESET}')
      continue
    parts.append(f'{_YELLOW}⚡ summoning {active.target} {age} ({_trail(active.trail_id)}){_RESET}')
  last = status.last
  if last is not None and now - last.ended_at < _LAST_OUTCOME_TTL:
    color = _GREEN if last.outcome == 'ok' else _RED
    mark = '✓' if last.outcome == 'ok' else '✗'
    parts.append(f'{color}{mark} summon {last.target}: {last.outcome}{_RESET}')
  return parts


def statusline() -> int:
  try:
    sys.stdin.read()  # drain the status JSON Claude pipes in; unused
  except OSError:
    pass
  parts = []
  if os.environ.get('RIDE_WORKSPACE') is not None and os.environ.get('RIDE_REPO') is None:
    parts.append(f'{_DIM}no repository attached{_RESET}')
  problem = health.problem()
  if problem is not None:
    parts.append(f'{_RED}⚠ session recording {problem}{_RESET}')
  parts.extend(_summon_parts(time.time()))
  if len(parts) > 0:
    print(' · '.join(parts))
  return 0


def main(argv: list[str]) -> Optional[int]:
  del argv
  return statusline()


if __name__ == '__main__':
  sys.exit(main(sys.argv))
