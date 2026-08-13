from pathlib import Path
from unittest.mock import patch

import pytest

from bro.trails.local import LocalStore
from bro.trails.network import NetworkStore
from bro.trails.store import build_store, local_root


def test_missing_backend_keeps_selecting_service():
  store = build_store({'base_url': 'https://trails.example', 'token': 'secret'})
  assert isinstance(store, NetworkStore)


def test_explicit_local_backend_uses_environment_root(tmp_path, monkeypatch):
  monkeypatch.setenv('BRO_TRAILS_DIR', str(tmp_path))
  store = build_store({'backend': 'local'})
  assert isinstance(store, LocalStore)
  assert store.root == tmp_path.resolve()


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


def test_default_local_root_uses_xdg_data_home(tmp_path, monkeypatch):
  monkeypatch.delenv('BRO_TRAILS_DIR', raising=False)
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  assert local_root() == Path(tmp_path) / 'bro'


def test_default_store_still_requires_the_trails_credential():
  from bro.trails.store import default_store

  with patch('bro.trails.store.credentials.get_json', side_effect=RuntimeError('missing')):
    with pytest.raises(RuntimeError, match='missing'):
      default_store()
