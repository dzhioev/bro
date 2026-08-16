from typing import Optional, cast
from unittest.mock import patch

import pytest

from bro.base import credentials
from bro.trails.local import LocalStore
from bro.trails.network import NetworkStore
from bro.trails.store import (
  build_store,
  configured_store,
  default_store,
  local_root,
  resolve_config,
  selects_local_storage,
)


def test_missing_backend_keeps_selecting_service():
  store = build_store({'base_url': 'https://trails.example', 'token': 'secret'})
  assert isinstance(store, NetworkStore)


def test_explicit_local_backend_uses_the_project_root(tmp_path, monkeypatch):
  monkeypatch.setattr('bro.trails.store.paths.project_root', lambda: tmp_path)
  store = build_store({'backend': 'local'})
  assert isinstance(store, LocalStore)
  assert store.root == (tmp_path / 'var' / 'cw' / 'trails').resolve()


def test_dynamo_backend_dispatches_through_the_server_package(monkeypatch):
  sentinel = object()
  config = {
    'backend': 'dynamo',
    'trails_table': 'headers',
    'steps_table': 'steps',
    'uuid_index': 'uuid-index',
    'bucket': 'spill',
    'region': 'eu-test-1',
  }
  monkeypatch.setattr('bro.trails.server.dynamo.build_dynamo_store', lambda value: sentinel)

  assert build_store(config) is sentinel


def test_unknown_backend_fails():
  with pytest.raises(ValueError, match='unknown trails backend'):
    build_store({'backend': 'other'})


def test_the_local_root_sits_beside_the_projects_other_state(tmp_path, monkeypatch):
  monkeypatch.setattr('bro.trails.store.paths.project_root', lambda: tmp_path)
  assert local_root() == tmp_path / 'var' / 'cw' / 'trails'


def _credential_store(config: Optional[dict]) -> credentials.Store:
  class _Store:
    def available(self, name: str) -> bool:
      assert name == 'trails'
      return config is not None

    def get_json(self, name: str) -> dict:
      assert name == 'trails'
      assert config is not None
      return config

  return cast(credentials.Store, _Store())


def test_an_absent_credential_resolves_to_local_storage():
  store = _credential_store(None)

  assert resolve_config(store) == {'backend': 'local'}
  assert selects_local_storage(store)


def test_a_configured_credential_resolves_to_its_own_backend():
  store = _credential_store({'base_url': 'https://trails.example', 'token': 'secret'})

  assert resolve_config(store) == {'base_url': 'https://trails.example', 'token': 'secret'}
  assert not selects_local_storage(store)


def test_default_store_records_locally_without_the_trails_credential(tmp_path, monkeypatch):
  monkeypatch.setattr('bro.trails.store.paths.project_root', lambda: tmp_path)
  with patch('bro.trails.store.credentials.default_store', return_value=_credential_store(None)):
    store = default_store()

  assert isinstance(store, LocalStore)
  assert store.root == (tmp_path / 'var' / 'cw' / 'trails').resolve()


def test_configured_store_builds_the_named_backend():
  with patch('bro.trails.store.credentials.get_json', return_value={'backend': 'dynamo'}) as read:
    with patch('bro.trails.server.dynamo.build_dynamo_store', lambda config: 'DYNAMO'):
      assert configured_store() == 'DYNAMO'
  read.assert_called_once_with('trails')


def test_configured_store_requires_the_trails_credential():
  with patch(
    'bro.trails.store.credentials.get_json', side_effect=credentials.SecretNotFound('trails')
  ):
    with pytest.raises(credentials.SecretNotFound):
      configured_store()
