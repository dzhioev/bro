"""the session's watch commands, matched where a shell command line invokes them:
its first word past any leading variable assignments."""

import re


def _invocation(program: str) -> re.Pattern[str]:
  return re.compile(rf'^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*{re.escape(program)}(?:\s|$)')


WATCH_RUN = _invocation('watch-run')
WATCH_NEXT = _invocation('watch-next')
