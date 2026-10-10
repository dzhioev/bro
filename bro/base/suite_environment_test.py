import os

import bro.harness as harness_registry
from bro.base import configs, credentials
from bro.base.suite_environment import (
  ABSENT_CREDENTIAL_STORE,
  host_credential_store,
  rebuild_environment,
)
from bro.harness import Harness, OwnedEnvironment


class _StatefulHarness(Harness):
  name = 'stateful'

  def owned_environment(self) -> OwnedEnvironment:
    return OwnedEnvironment(namespaces=('STATEFUL_',), variables=('STATEFUL',))


class _EntryPoint:
  name = _StatefulHarness.name

  def load(self) -> Harness:
    return _StatefulHarness()


def test_the_rebuild_clears_what_an_installed_harness_owns(monkeypatch):
  monkeypatch.setattr(harness_registry, '_entry_points', lambda: (_EntryPoint(),))
  monkeypatch.setenv('STATEFUL_CONFIG_DIR', '/session/config')
  monkeypatch.setenv('STATEFUL', '1')
  monkeypatch.setenv('STATEFULNESS', 'unowned')
  rebuild_environment()
  assert 'STATEFUL_CONFIG_DIR' not in os.environ
  assert 'STATEFUL' not in os.environ
  assert os.environ['STATEFULNESS'] == 'unowned'


def test_the_host_store_resolves_only_inside_the_block(tmp_path, monkeypatch):
  monkeypatch.setattr(configs, 'STORE_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, '_default_store', None)
  material = tmp_path / credentials.MATERIAL_DIR / 'openai.cred'
  material.parent.mkdir()
  material.write_text('host-key')
  assert not credentials.available('openai')
  with host_credential_store():
    assert credentials.get('openai') == 'host-key'
  assert credentials.STORE_DIR == ABSENT_CREDENTIAL_STORE
  assert not credentials.available('openai')
