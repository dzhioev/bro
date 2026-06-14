#!/usr/bin/env python
"""Claude Code statusLine: a red warning when session-log sync is failing.

Prints nothing when healthy so Claude keeps its default status bar, and pins a
red `⚠ session-log sync FAILING` line when the health file reports an error —
the one coloured channel that survives Claude's alternate-screen buffer.
"""

import sys

import session_log_health

__cli_name__ = 'session-log-statusline'


def statusline() -> int:
  try:
    sys.stdin.read()  # drain the status JSON Claude pipes in; unused
  except OSError:
    pass
  if session_log_health.is_failing():
    print('\033[1;31m⚠ session-log sync FAILING — run setup/bootstrap_session_log.sh\033[0m')
  return 0


def main(argv=None):
  return statusline()


if __name__ == '__main__':
  sys.exit(main(sys.argv))
