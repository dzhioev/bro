from unittest.mock import patch

import pytest

import ride.claude.claude_auth as ride_claude_auth


@pytest.fixture
def material_dir(monkeypatch, tmp_path):
  from bro.base import credentials

  monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, '_default_store', None)
  material = tmp_path / credentials.MATERIAL_DIR
  material.mkdir()
  return material


class TestApplyClaudeAuth:
  def test_present_exports_token(self, material_dir):
    # the `claude_code` secret is a scalar token file; the store strips it.
    (material_dir / 'claude_code.cred').write_text('oauth-tok\n')
    env: dict[str, str] = {}
    ride_claude_auth.apply_claude_auth(env)
    assert env == {'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}

  def test_absent_leaves_env_unchanged(self, material_dir):
    env: dict[str, str] = {}
    ride_claude_auth.apply_claude_auth(env)
    assert env == {}

  def test_absent_warns_when_requested(self, material_dir):
    with patch('ride.claude.claude_auth.log.warning') as warning:
      ride_claude_auth.apply_claude_auth({}, warn_when_missing=True)
    warning.assert_called_once()
    assert 'claude_code secret not resolvable' in warning.call_args.args[0]
    assert warning.call_args.args[1:] == (material_dir / 'claude_code.cred',)

  def test_scrubs_outranking_auth_vars(self, material_dir):
    # inherited api-key / bearer vars outrank CLAUDE_CODE_OAUTH_TOKEN in claude's
    # credential precedence, so they must not leak into the session
    (material_dir / 'claude_code.cred').write_text('oauth-tok')
    env = {
      'ANTHROPIC_API_KEY': 'sk-ant-stale',
      'ANTHROPIC_AUTH_TOKEN': 'stale-bearer',
      'UNRELATED': 'kept',
    }
    ride_claude_auth.apply_claude_auth(env)
    assert env == {'UNRELATED': 'kept', 'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}

  def test_overwrites_inherited_stale_token(self, material_dir):
    # a CLAUDE_CODE_OAUTH_TOKEN exported by the launching shell loses to the
    # freshly resolved secret
    (material_dir / 'claude_code.cred').write_text('oauth-tok')
    env = {'CLAUDE_CODE_OAUTH_TOKEN': 'stale-tok'}
    ride_claude_auth.apply_claude_auth(env)
    assert env == {'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}
