"""Claude Code statusLine: session warnings and live summon state.

Prints nothing when there is nothing to say, so Claude keeps its default status
bar. Two sections, joined into the one line Claude renders:

- a red `⚠ session recording …` warning when the health file reports a failing or
  a stopped recorder — the one coloured channel that survives Claude's
  alternate-screen buffer;
- the session's summons, read from the status file `CW_SUMMON_STATUS` points at
  (written host-side by `bro/launch/summon_control.py`): each active summon as target, trail id,
  and age, plus the last terminal outcome for a while after it lands. External
  stderr is invisible under the fullscreen TUI, so this is the session's one live
  view of an in-flight summon.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from bro.monitor import health
from bro.summon import STATUS_ENV

# how long the last terminal outcome stays on the status line; after that the
# default status bar comes back
_LAST_OUTCOME_TTL = 900.0

_RED = '\033[1;31m'
_YELLOW = '\033[1;33m'
_GREEN = '\033[1;32m'
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
  status_path = os.environ.get(STATUS_ENV)
  if status_path is None:
    return []
  path = Path(status_path)
  if not path.is_file():
    return []
  try:
    status: Any = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError):
    return [f'{_RED}⚠ summon status unreadable{_RESET}']
  parts = []
  for active in status.get('active', []):
    age = _age(now - active.get('started_at', now))
    parts.append(
      f'{_YELLOW}⚡ summoning {active.get("target")} {age} ({_trail(active.get("trail_id"))}){_RESET}'
    )
  last = status.get('last')
  if last is not None and now - last.get('ended_at', 0) < _LAST_OUTCOME_TTL:
    outcome = last.get('outcome')
    color = _GREEN if outcome == 'ok' else _RED
    mark = '✓' if outcome == 'ok' else '✗'
    parts.append(f'{color}{mark} summon {last.get("target")}: {outcome}{_RESET}')
  return parts


def statusline() -> int:
  try:
    sys.stdin.read()  # drain the status JSON Claude pipes in; unused
  except OSError:
    pass
  parts = []
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
