from typing import Optional, cast
from unittest.mock import patch

import pytest

from bro.base import credentials
from bro.trails.local import LocalStore
from bro.trails.network import NetworkStore
from bro.trails.store import (
  TrailsStore,
  build_store,
  configured_store,
  default_store,
  local_root,
  resolve_config,
  selects_local_storage,
)
from bro.workspace import paths as workspace_paths


def test_missing_backend_keeps_selecting_service():
  store = build_store({'base_url': 'https://trails.example', 'token': 'secret'})
  assert isinstance(store, NetworkStore)


def test_explicit_local_backend_uses_the_project_root(tmp_path, monkeypatch):
  monkeypatch.setattr('bro.trails.store.paths.project_root', lambda: tmp_path)
  store = build_store({'backend': 'local'})
  assert isinstance(store, LocalStore)
  assert store.root == workspace_paths.trails_dir().resolve()


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
  assert local_root() == workspace_paths.trails_dir()


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
  assert store.root == workspace_paths.trails_dir().resolve()


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


class _CappedPages:
  """serves at most two rows per page whatever the limit asks, the way a
  size-capped backend does."""

  def __init__(self, count: int):
    self.rows = [{'step_id': step_id} for step_id in range(count)]
    self.calls: list[tuple[Optional[int], int]] = []

  def get_steps(
    self, trail_id: str, *, after: Optional[int] = None, limit: Optional[int] = None
  ) -> dict:
    assert limit is not None
    self.calls.append((after, limit))
    start = 0 if after is None else after + 1
    page = self.rows[start : start + min(limit, 2)]
    through = page[-1]['step_id'] if len(page) > 0 else None
    more = start + len(page) < len(self.rows)
    return {'steps': page, 'next': through if more else None, 'through': through}


def test_collect_steps_pages_every_window_to_its_end_and_no_further():
  backend = _CappedPages(7)
  store = cast(TrailsStore, backend)

  assert TrailsStore.collect_steps(store, 'T', extent=7, window=3) == backend.rows

  assert len(backend.calls) == 5
  assert set(backend.calls) == {(None, 3), (1, 1), (2, 3), (4, 1), (5, 1)}


def test_collect_steps_stops_at_the_extent():
  backend = _CappedPages(7)

  assert TrailsStore.collect_steps(cast(TrailsStore, backend), 'T', extent=4) == backend.rows[:4]


def test_collect_steps_refuses_rows_that_run_out_before_the_extent():
  backend = _CappedPages(5)

  with pytest.raises(RuntimeError, match='ran out after step 4, wanted them through step 5'):
    TrailsStore.collect_steps(cast(TrailsStore, backend), 'T', extent=7, window=3)


def test_collect_steps_refuses_a_page_that_does_not_advance():
  class _StuckPage:
    def get_steps(
      self, trail_id: str, *, after: Optional[int] = None, limit: Optional[int] = None
    ) -> dict:
      through = -1 if after is None else after
      return {'steps': [], 'next': through, 'through': through}

  with pytest.raises(RuntimeError, match='not past step -1'):
    TrailsStore.collect_steps(cast(TrailsStore, _StuckPage()), 'T', extent=3)
