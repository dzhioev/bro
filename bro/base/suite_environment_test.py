from bro.base import configs, credentials
from bro.base.suite_environment import ABSENT_CREDENTIAL_STORE, host_credential_store


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
