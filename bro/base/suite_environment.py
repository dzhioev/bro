"""the rebuild that makes a pytest run hermetic against the session it starts in.

The environment is rebuilt rather than patched: every variable in the
framework's own namespaces and in each installed harness's is cleared before
collection, and a test needing one sets it itself. A suite launched from inside
a managed session would otherwise inherit the running session's broker channel,
hold, credential scope, workspace, harness state and state dir — and through
the state dir, write it: the terminating service tools leave the status their
session is to report there (`bro/workspace/session.py`), so a test exercising
them would decide the exit status of the session running the suite. Clearing by
namespace rather than by name is what keeps the next variable the framework
invents from having to be discovered the same way.

One fixed variable carries session state without living in those namespaces and is
named on its own: `MCP_SERVER_BEARER_TOKEN`, the session-local MCP server's own credential.
Credential install hooks may export variables in a tool's own namespace;
the rebuild discovers those declarations from the installed registry and clears them too.
A harness names the environment its sessions carry its state in through its
`owned_environment()`, and the rebuild loads every installed harness to clear it.

The credential resolver's exclusive directory is session state no sweep can
reach once `bro.base.configs` has captured `BRO_STORE`.
Left alone, a run resolves whatever the operator holds or whatever its managed
session hydrated, and `credentials.available` then decides which components a
bro's feature gate composes.
The module constant is pinned to an absent path and the process-wide store is
dropped, so a suite resolves nothing and a test that means a credential
installs its own store.
`host_credential_store` is the way back for a test that deliberately asks what
the host holds.

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
from bro.harness import OwnedEnvironment, get_harness, installed_harness_names


def _credential_install_variables() -> tuple[str, ...]:
  return tuple(
    variable
    for secret in credentials.default_registry().values()
    if secret.install is not None
    for variable in secret.install.get('env', {})
  )


FRAMEWORK_ENVIRONMENT = OwnedEnvironment(
  namespaces=('BROKER_', 'BRO_', 'CREDENTIALS_', 'RIDE_', 'TRAILS_'),
  variables=('MCP_SERVER_BEARER_TOKEN', *_credential_install_variables()),
)
TIMEZONE = 'Asia/Kolkata'

# The absent exclusive store a suite resolves against.
ABSENT_CREDENTIAL_STORE = os.path.join(tempfile.gettempdir(), 'bro-suite-absent-credential-store')


def session_environment() -> OwnedEnvironment:
  """every variable carrying session state: the framework's own and each installed harness's."""
  owners = [
    FRAMEWORK_ENVIRONMENT,
    *(get_harness(name).owned_environment() for name in installed_harness_names()),
  ]
  return OwnedEnvironment(
    namespaces=tuple(namespace for owner in owners for namespace in owner.namespaces),
    variables=tuple(variable for owner in owners for variable in owner.variables),
  )


def rebuild_environment() -> None:
  session = session_environment()
  for name in [name for name in os.environ if session.owns(name)]:
    del os.environ[name]
  credentials.STORE_DIR = ABSENT_CREDENTIAL_STORE
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
  pinned_store_dir = credentials.STORE_DIR
  credentials.STORE_DIR = configs.STORE_DIR
  credentials._default_store = None
  try:
    yield
  finally:
    credentials.STORE_DIR = pinned_store_dir
    credentials._default_store = None
