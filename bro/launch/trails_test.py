from bro.launch import trails as launch_trails
from bro.workspace.store import ScopedSecrets


def _scope(required):
  return ScopedSecrets(set(required), {'openai'}, False)


def test_scope_without_trails_adds_no_launch_data(monkeypatch):
  def unexpected_read(names, *, optional=()):
    raise AssertionError(f'unexpected scope read: {names}, {optional}')

  monkeypatch.setattr(launch_trails.credentials, 'scoped_view_store', unexpected_read)

  assert launch_trails.local_trails_launch_data(_scope({'github'})) == ({}, ())


def test_service_credential_adds_no_launch_data(monkeypatch):
  class Store:
    def get_json(self, name):
      assert name == 'trails'
      return {'base_url': 'https://trails.example', 'token': 'secret'}

  monkeypatch.setattr(
    launch_trails.credentials,
    'scoped_view_store',
    lambda names, optional=(): Store(),
  )

  assert launch_trails.local_trails_launch_data(_scope({'github', 'trails'})) == ({}, ())


def test_local_credential_maps_the_host_root_for_a_selected_instance(monkeypatch, tmp_path):
  selected = []

  class Store:
    def get_json(self, name):
      assert name == 'trails'
      return {'backend': 'local'}

  def scoped_view_store(names, *, optional=()):
    selected.append((set(names), set(optional)))
    return Store()

  root = tmp_path / 'trail-data'
  monkeypatch.setattr(launch_trails.credentials, 'scoped_view_store', scoped_view_store)
  monkeypatch.setattr(launch_trails, 'local_root', lambda: root)

  environment, mounts = launch_trails.local_trails_launch_data(_scope({'github', 'trails+eu'}))

  assert selected == [({'github', 'trails+eu'}, {'openai'})]
  assert environment == {}
  assert mounts == (f'{root.resolve()}:/workspace/var/cw/trails',)
  assert root.is_dir()
