from pathlib import Path
from unittest.mock import patch

import pytest

from bro.trails.client import TrailsClient
from bro.trails.local import LocalStore
from bro.trails.store import build_store, local_root


def test_missing_backend_keeps_selecting_service():
  store = build_store({'base_url': 'https://trails.example', 'token': 'secret'})
  assert isinstance(store, TrailsClient)


def test_explicit_local_backend_uses_environment_root(tmp_path, monkeypatch):
  monkeypatch.setenv('BRO_TRAILS_DIR', str(tmp_path))
  store = build_store({'backend': 'local'})
  assert isinstance(store, LocalStore)
  assert store.root == tmp_path.resolve()


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
