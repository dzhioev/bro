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

The credential resolver's local search path is session state no sweep can
reach: it is two module constants, one captured from `BRO_CONFIGS_DIR` when
`bro.base.configs` is imported and the other the host's own `~/.bro`. Left
alone, a run resolves whatever the operator holds — or, inside a managed
session, whatever that session's scoped store hydrated — and
`credentials.available` then decides which components a bro's feature gate
composes. Both roots are pinned away and the process-wide store dropped, so a
suite resolves nothing and a test that means a credential installs its own
store. `host_credential_store` is the way back, for a test that deliberately
asks what the host holds.

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

import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Iterator

from bro.base import configs, credentials, log

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

# where both of the resolver's local search roots are pinned: a directory that
# does not exist, so nothing along the path resolves and nothing accumulates
# between runs.
ABSENT_CREDENTIAL_STORE = os.path.join(tempfile.gettempdir(), 'bro-suite-absent-credential-store')


def _carries_session_state(name: str) -> bool:
  return (
    name.startswith(SESSION_NAMESPACES) or name in SESSION_VARIABLES
  ) and name not in KEPT_VARIABLES


def rebuild_environment() -> None:
  for name in [name for name in os.environ if _carries_session_state(name)]:
    del os.environ[name]
  credentials.CONFIGS_DIR = ABSENT_CREDENTIAL_STORE
  credentials.BRO_DIR = ABSENT_CREDENTIAL_STORE
  credentials._default_store = None
  os.environ['TZ'] = TIMEZONE
  time.tzset()
  log.set_level(logging.INFO)
  os.environ.pop(log.LEVEL_ENV, None)


@contextlib.contextmanager
def host_credential_store() -> Iterator[None]:
  """the operator's own credential store, in place of the pin, for the block.

  For a test that has to ask what the host holds — one whose subject resolves
  those credentials outside this process. The process-wide store is dropped on
  both ends, so neither side of the block serves the other's registry.
  """
  pinned_configs_dir, pinned_bro_dir = credentials.CONFIGS_DIR, credentials.BRO_DIR
  credentials.CONFIGS_DIR = configs.BRO_CONFIGS_DIR
  credentials.BRO_DIR = configs.DEFAULT_BRO_DIR
  credentials._default_store = None
  try:
    yield
  finally:
    credentials.CONFIGS_DIR, credentials.BRO_DIR = pinned_configs_dir, pinned_bro_dir
    credentials._default_store = None
