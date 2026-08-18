"""project-wide pytest config.

pinned to a no-op `Tracker` so tests never try to ship trail data to a
configured sink — even on a workstation where the production recorder is the
default via `set_default_tracker_factory`.

`BROKER_CHANNEL` is dropped for the same reason: every broker-supervised ride
session (container and host worktree alike) carries the env var, and a bro
run in a test would otherwise connect to the live session channel and emit
lifecycle events into it. Unset, `BroChannel.from_env()` returns None and
the hook is inert.

`BRO_HOLD` and `RIDE_RUNNER_PID` are dropped for the same hermeticity: together
they gate the `raise` service tool's claude mounts, and tests would otherwise
see the launching session's values.

`CREDENTIALS_REGISTRY` is dropped so credential tests are hermetic: a host ride
session exports it pointing at the session's scoped store, which outranks the
`BRO_DIR` / `CONFIGS_DIR` module attributes the tests redirect to tmp dirs —
secrets would resolve against the session's scoped set instead of the test
fixture. Tests exercising the override set it themselves via monkeypatch.

The run-start credential gate is pinned open for the same hermeticity (the
autouse fixture below): test bros mostly keep the default openai spec, whose
`openai` key would otherwise gate on the host's real store. Gate tests opt out
with `@pytest.mark.credential_gate` and control `credentials.available`
themselves.

`~/.bro.json` is pointed at an absent path (the autouse fixture below): it maps
the developer's own checkouts to credential instances, and a launch-scoping test
runs from inside one of them, so the real file would bind the resolver to
whatever that checkout reads.

The usage-file pointer and the Claude session id are dropped before every test
(the autouse fixture below): a test suite launched from inside a bro run or a
claude session inherits the live usage source, and `usage.publish` exports a
minted pointer into `os.environ` mid-run, so a test reading a usage source
would otherwise see the launching session's spend or an earlier test's file. `PWD` is dropped at import — the
transcript fallback resolves the working directory through it, and
`monkeypatch.chdir` never updates it, so a chdir'd test would still read the
launching session's transcripts. `RIDE_WORKSPACE` too — it marks a managed workspace,
and a bro run in a test would otherwise provision the launching session's
workspace (`BaseBro._provision_workspace`). `RIDE_IN_CONTAINER` is dropped so
path tests resolve host runtime roots unless they explicitly exercise the fixed
container mounts.

The log level is pinned to INFO and `BRO_LOG_LEVEL` dropped — at session start
against an inherited verbose launch (`run-tests --verbose`), and after every
test (the autouse fixture below) against a test that parses `--log`/`--verbose`
through a real CLI: level-sensitive tests — including every subprocess a test
spawns — must see the default they assert against.

`*_llm_test.py` files are live-LLM behavior probes: they run a real bro against
the configured provider and spend real tokens, so they stay outside the default
roster and `pytest_collection_modifyitems` below skips them unless
`BRO_LLM_TESTS=1` explicitly opts in.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest

import bro.llm.usage as usage
from bro.base import log
from bro.base.credentials import REGISTRY_ENV
from bro.bro import set_default_tracker_factory
from bro.broker.client import CHANNEL_ENV
from bro.launch.hold import HOLD_VARIABLE
from bro.llm.tracker import NullTracker

set_default_tracker_factory(NullTracker)
os.environ.pop(CHANNEL_ENV, None)
os.environ.pop(HOLD_VARIABLE, None)
os.environ.pop('RIDE_RUNNER_PID', None)
os.environ.pop(REGISTRY_ENV, None)
os.environ.pop('PWD', None)
os.environ.pop('RIDE_WORKSPACE', None)
os.environ.pop('RIDE_IN_CONTAINER', None)
log.set_level(logging.INFO)
os.environ.pop(log.LEVEL_ENV, None)


def pytest_collection_modifyitems(items):
  if os.environ.get('BRO_LLM_TESTS') == '1':
    return
  skip = pytest.mark.skip(
    reason='live-LLM behavior probe; spends real tokens — set BRO_LLM_TESTS=1 to run'
  )
  for item in items:
    if item.path.name.endswith('_llm_test.py'):
      item.add_marker(skip)


@pytest.fixture(autouse=True)
def _pin_credential_gate_open(request, monkeypatch):
  if request.node.get_closest_marker('credential_gate') is None:
    monkeypatch.setattr('bro.bro.BaseBro.missing_secrets', lambda self: ())


@pytest.fixture(autouse=True)
def _isolate_host_config(monkeypatch, tmp_path):
  monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(tmp_path / 'absent.json'))
  monkeypatch.setattr('bro.base.credentials._selected_instances', {})


@pytest.fixture(autouse=True)
def _isolate_runtime_state(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path.parent / f'{tmp_path.name}-ride-state'))


@pytest.fixture(autouse=True)
def _drop_usage_source():
  os.environ.pop(usage.USAGE_FILE_VARIABLE, None)
  os.environ.pop(usage.SESSION_ID_VARIABLE, None)


@pytest.fixture(autouse=True)
def _reset_log_level():
  yield
  log.set_level(logging.INFO)
  os.environ.pop(log.LEVEL_ENV, None)


@pytest.fixture
def register_test_bros():
  import bro.registry

  registered_names: list[str] = []

  def register(*bro_classes):
    for bro_class in bro_classes:
      bro.registry.register(bro_class)
      registered_names.append(bro_class.name)

  yield register

  for name in registered_names:
    del bro.registry._REGISTRY[name]


@pytest.fixture
def socket_dir():
  """a short-path tempdir for tests that bind AF_UNIX sockets.

  sun_path caps at ~104 bytes on macOS (108 on Linux), and pytest's tmp_path —
  the resolved system temp plus per-test naming — exceeds it on macOS before a
  socket name is even appended. /tmp keeps the whole path well under the cap.
  """
  path = Path(tempfile.mkdtemp(prefix='sk-', dir='/tmp'))
  yield path
  shutil.rmtree(path, ignore_errors=True)
