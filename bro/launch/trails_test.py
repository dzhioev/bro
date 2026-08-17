from bro.launch import trails as launch_trails
from bro.workspace.store import ScopedSecrets


def _scope(required, optional=('openai',)):
  return ScopedSecrets(set(required), set(optional), False)


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
  returns the (required, optional) name sets the view store was built over."""
  reads = []

  def scoped_view_store(names, *, optional=()):
    reads.append((set(names), set(optional)))
    return _Store(config)

  monkeypatch.setattr(launch_trails.credentials, 'scoped_view_store', scoped_view_store)
  return reads


def test_a_scope_without_a_trails_credential_maps_the_host_root(monkeypatch, tmp_path):
  root = tmp_path / 'trail-data'
  _patch_view_store(monkeypatch, None)
  monkeypatch.setattr(launch_trails, 'local_root', lambda: root)

  mounts = launch_trails.local_trails_mounts(_scope({'github'}))

  assert mounts == (f'{root.resolve()}:/var/ride/trails',)
  assert root.is_dir()


def test_a_service_credential_maps_nothing(monkeypatch):
  _patch_view_store(monkeypatch, {'base_url': 'https://trails.example', 'token': 'secret'})

  assert launch_trails.local_trails_mounts(_scope({'github', 'trails'})) == ()


def test_a_local_credential_maps_the_host_root_for_a_selected_instance(monkeypatch, tmp_path):
  root = tmp_path / 'trail-data'
  reads = _patch_view_store(monkeypatch, {'backend': 'local'})
  monkeypatch.setattr(launch_trails, 'local_root', lambda: root)

  mounts = launch_trails.local_trails_mounts(_scope({'github'}, optional={'trails+eu'}))

  assert reads == [({'github'}, {'trails+eu'})]
  assert mounts == (f'{root.resolve()}:/var/ride/trails',)
  assert root.is_dir()
