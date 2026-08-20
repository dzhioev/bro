"""project-wide pytest config.

pinned to a no-op `Tracker` so tests never try to ship trail data to a
configured sink — even on a workstation where the production recorder is the
default via `set_default_tracker_factory`.

The run-start credential gate is pinned open for the same hermeticity (the
autouse fixture below): test bros mostly keep the default openai spec, whose
`openai` key would otherwise gate on the host's real store. Gate tests opt out
with `@pytest.mark.credential_gate` and control `credentials.available`
themselves.

`~/.bro.json` is pointed at an absent path (the autouse fixture below): it maps
the developer's own checkouts to credential instances, and a launch-scoping test
runs from inside one of them, so the real file would bind the resolver to
whatever that checkout reads.

Two variables `rebuild_environment` sweeps are also dropped or pinned *between*
tests, against leakage a once-per-run rebuild cannot reach (the autouse fixtures
below): `usage.publish` mints a usage-file pointer into `os.environ` mid-run, so
a later test would read an earlier one's file; and a test parsing `--log` /
`--verbose` through a real CLI sets the log level, which the next
level-sensitive test — including every subprocess it spawns — must not see.

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
from bro.base.suite_environment_test_helper import rebuild_environment
from bro.llm.tracker import NullTracker
from bro.native.runner import set_default_tracker_factory

set_default_tracker_factory(NullTracker)
rebuild_environment()


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
