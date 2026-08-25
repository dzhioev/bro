"""project-wide pytest config.

pinned to a no-op `Tracker` so tests never try to ship trail data to a
configured sink — even on a workstation where the production recorder is the
default via `set_default_tracker_factory`.

The run-start credential gate is pinned open for the same hermeticity (the
autouse fixture below): test bros mostly keep the default openai spec, and no
suite resolves its `openai` key — `rebuild_environment` pins the credential
search path away from every store. Gate tests opt out with
`@pytest.mark.credential_gate` and control `credentials.available` themselves.

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

import pytest

import bro.llm.usage as usage
from bro.base import log
from bro.base.suite_environment import rebuild_environment, token_spending_skip_reason
from bro.llm.tracker import NullTracker
from bro.native.runner import set_default_tracker_factory

set_default_tracker_factory(NullTracker)
rebuild_environment()


def pytest_collection_modifyitems(items):
  reason = token_spending_skip_reason()
  if reason is None:
    return
  skip = pytest.mark.skip(reason=f'live-LLM behavior probe; {reason}')
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
