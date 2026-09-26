import json

from bro.base import credentials
from ride import trails
from ride.workspace.store import ScopedSecrets


def _scope(required, optional=('openai',), selection=None):
  return ScopedSecrets(set(required), set(optional), selection or {})


class _Store:
  def __init__(self, config):
    self._config = config

  def available(self, name):
    assert name == 'trails'
    return self._config is not None

  def get_json(self, name):
    assert name == 'trails'
    return self._config


def _patch_view_store(monkeypatch, config):
  """serve `config` as the scope's trails credential (None = no such credential);
  returns the required and optional kinds plus the selection the view was built over."""
  reads = []

  def scoped_view_store(store, names, *, optional=()):
    reads.append((set(names), set(optional), dict(store.selection)))
    return _Store(config)

  monkeypatch.setattr(trails.credentials, 'scoped_view_store', scoped_view_store)
  return reads


def test_a_scope_without_a_trails_credential_maps_the_host_root(monkeypatch, tmp_path):
  root = tmp_path / 'trail-data'
  _patch_view_store(monkeypatch, None)
  monkeypatch.setattr(trails, 'local_root', lambda: root)

  mounts = trails.local_trails_mounts(_scope({'github'}))

  assert mounts == (f'{root.resolve()}:/var/ride/trails',)
  assert root.is_dir()


def test_a_service_credential_maps_nothing(monkeypatch):
  _patch_view_store(monkeypatch, {'base_url': 'https://trails.example', 'token': 'secret'})

  assert trails.local_trails_mounts(_scope({'github', 'trails'})) == ()


def test_the_store_default_selects_the_host_side_trails_view(monkeypatch, tmp_path):
  store = tmp_path / 'store'
  material = store / credentials.MATERIAL_DIR
  material.mkdir(parents=True)
  (material / 'trails+service.cred').write_text(
    json.dumps({'base_url': 'https://trails.example', 'token': 'secret'})
  )
  (store / credentials.STORE_FILE).write_text(json.dumps({'defaults': ['trails+service']}))
  monkeypatch.setattr(credentials, 'STORE_DIR', str(store))

  assert trails.local_trails_mounts(_scope({'trails'}, optional=())) == ()


def test_a_local_credential_maps_the_host_root_for_a_selected_instance(monkeypatch, tmp_path):
  root = tmp_path / 'trail-data'
  reads = _patch_view_store(monkeypatch, {'backend': 'local'})
  monkeypatch.setattr(trails, 'local_root', lambda: root)

  mounts = trails.local_trails_mounts(
    _scope({'github'}, optional={'trails'}, selection={'trails': 'eu'})
  )

  assert reads == [({'github'}, {'trails'}, {'trails': 'eu'})]
  assert mounts == (f'{root.resolve()}:/var/ride/trails',)
  assert root.is_dir()
