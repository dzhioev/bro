import json
from unittest.mock import patch

import pytest

import cw.claude_auth


@pytest.fixture
def config_path(monkeypatch, tmp_path):
  from base import credentials

  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, 'PPP_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, '_default_store', None)
  return tmp_path / 'anthropic.json'


class TestLoadAnthropicKey:
  def test_reads_from_config(self, config_path):
    config_path.write_text(json.dumps({'api_key': 'sk-from-file'}))
    assert cw.claude_auth._load_anthropic_key() == 'sk-from-file'

  def test_none_when_missing(self, config_path):
    assert cw.claude_auth._load_anthropic_key() is None

  def test_none_when_empty_value(self, config_path):
    config_path.write_text(json.dumps({'api_key': ''}))
    assert cw.claude_auth._load_anthropic_key() is None

  def test_none_when_field_missing(self, config_path):
    config_path.write_text(json.dumps({'something_else': 'x'}))
    assert cw.claude_auth._load_anthropic_key() is None


class TestApplyClaudeAuth:
  def test_present_exports_token(self, config_path):
    # the `claude_code` secret is a scalar token file; the store strips it.
    (config_path.parent / 'claude_code_oauth_token').write_text('oauth-tok\n')
    env: dict[str, str] = {}
    cw.claude_auth._apply_claude_auth(env)
    assert env == {'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}

  def test_absent_leaves_env_unchanged(self, config_path):
    env: dict[str, str] = {}
    cw.claude_auth._apply_claude_auth(env)
    assert env == {}

  def test_absent_warns_when_requested(self, config_path):
    with patch('cw.claude_auth.log.warning') as warning:
      cw.claude_auth._apply_claude_auth({}, warn_when_missing=True)
    warning.assert_called_once()
    assert 'claude_code secret not resolvable' in warning.call_args.args[0]

  def test_scrubs_outranking_auth_vars(self, config_path):
    # inherited api-key / bearer vars outrank CLAUDE_CODE_OAUTH_TOKEN in claude's
    # credential precedence, so they must not leak into the session
    (config_path.parent / 'claude_code_oauth_token').write_text('oauth-tok')
    env = {
      'ANTHROPIC_API_KEY': 'sk-ant-stale',
      'ANTHROPIC_AUTH_TOKEN': 'stale-bearer',
      'UNRELATED': 'kept',
    }
    cw.claude_auth._apply_claude_auth(env)
    assert env == {'UNRELATED': 'kept', 'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}

  def test_overwrites_inherited_stale_token(self, config_path):
    # a CLAUDE_CODE_OAUTH_TOKEN exported by the launching shell loses to the
    # freshly resolved secret
    (config_path.parent / 'claude_code_oauth_token').write_text('oauth-tok')
    env = {'CLAUDE_CODE_OAUTH_TOKEN': 'stale-tok'}
    cw.claude_auth._apply_claude_auth(env)
    assert env == {'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}
