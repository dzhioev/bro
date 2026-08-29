from bro.base import configs, credentials
from bro.base.suite_environment import (
  ABSENT_CREDENTIAL_STORE,
  TOKENS_OPT_IN,
  host_credential_store,
  token_spending_skip_reason,
)


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


class TestTokenSpendingSkipReason:
  def test_the_opt_in_lets_a_spender_run(self, monkeypatch):
    monkeypatch.setenv(TOKENS_OPT_IN, '1')

    assert token_spending_skip_reason() is None

  def test_an_unset_opt_in_names_itself_in_the_reason(self, monkeypatch):
    monkeypatch.delenv(TOKENS_OPT_IN, raising=False)

    assert TOKENS_OPT_IN in str(token_spending_skip_reason())

  def test_any_other_value_withholds_the_run(self, monkeypatch):
    monkeypatch.setenv(TOKENS_OPT_IN, 'yes')

    assert token_spending_skip_reason() is not None
