"""the rebuild that makes a pytest run hermetic against the session it starts in.

The environment is rebuilt rather than patched: every variable in the
framework's own namespaces is cleared before collection, and a test needing one
sets it itself. A suite launched from inside a managed session would otherwise
inherit the running session's broker channel, hold, credential scope, workspace,
claude config and state dir — and through the state dir, write it: the
terminating service tools leave the status their session is to report there
(`bro/workspace/session.py`), so a test exercising them would decide the exit
status of the session running the suite. Clearing by namespace rather than by
name is what keeps the next variable the framework invents from having to be
discovered the same way. `BRO_LLM_TESTS` is the one name kept: an opt-in a
caller passes the run deliberately rather than session state it stands in.

Three variables carry session state without living in those namespaces and are
named one by one: `PWD`, which the transcript fallback resolves the working
directory through while `monkeypatch.chdir` never updates it, so a chdir'd test
would still read the launching session's transcripts; `MCP_SERVER_BEARER_TOKEN`,
the session-local MCP server's own credential; and `AI_AGENT`, which claude code
exports and `usage.claude_version` parses the running harness's version out of.

Rendered timestamps resolve through the host zone (`datetime.astimezone()`), so
the suite pins one: unpinned, a display assertion holds only where the developer
sits; pinned to UTC, it stops catching a dropped conversion. The zone has a
half-hour offset and no DST, so the rendered values are stable and no whole-hour
assumption passes. `time.tzset()` is what makes libc read the variable — glibc
does not re-read it on its own.

The logger takes its level from the environment when `bro.base.log` is imported,
which happens before the sweep can reach the variable, so the level is restored
along with it.
"""

import logging
import os
import time

from bro.base import log

SESSION_NAMESPACES = (
  'ANTHROPIC_',
  'BROKER_',
  'BRO_',
  'CLAUDE_',
  'CREDENTIALS_',
  'RIDE_',
  'TRAILS_',
)
SESSION_VARIABLES = frozenset({'AI_AGENT', 'MCP_SERVER_BEARER_TOKEN', 'PWD'})
KEPT_VARIABLES = frozenset({'BRO_LLM_TESTS'})
TIMEZONE = 'Asia/Kolkata'


def _carries_session_state(name: str) -> bool:
  return (
    name.startswith(SESSION_NAMESPACES) or name in SESSION_VARIABLES
  ) and name not in KEPT_VARIABLES


def rebuild_environment() -> None:
  for name in [name for name in os.environ if _carries_session_state(name)]:
    del os.environ[name]
  os.environ['TZ'] = TIMEZONE
  time.tzset()
  log.set_level(logging.INFO)
  os.environ.pop(log.LEVEL_ENV, None)
