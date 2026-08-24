import json

from bro.base import configs, credentials
from bro.base.suite_environment import ABSENT_CREDENTIAL_STORE, host_credential_store


def test_the_host_store_resolves_only_inside_the_block(tmp_path, monkeypatch):
  monkeypatch.setattr(configs, 'DEFAULT_BRO_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, '_default_store', None)
  (tmp_path / 'credentials.json').write_text(
    json.dumps({'openai': {'sources': [{'file': 'openai.cred'}]}})
  )
  (tmp_path / 'openai.cred').write_text('host-key')
  assert not credentials.available('openai')
  with host_credential_store():
    assert credentials.get('openai') == 'host-key'
  assert credentials.BRO_DIR == ABSENT_CREDENTIAL_STORE
  assert not credentials.available('openai')
