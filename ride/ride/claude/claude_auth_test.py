import json
from unittest.mock import MagicMock, patch

import pytest

import ride.claude.claude_auth as ride_claude_auth
from ride.workspace.store import ScopedSecrets


@pytest.fixture
def material_dir(monkeypatch, tmp_path):
  from bro.base import credentials

  monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, '_default_store', None)
  material = tmp_path / credentials.MATERIAL_DIR
  material.mkdir()
  return material


class TestClaudeAuthPreflight:
  def test_reads_the_launch_store_selection(self, tmp_path, monkeypatch):
    from bro.base import credentials
    from ride.claude.harness import CLAUDE

    material = tmp_path / credentials.MATERIAL_DIR
    material.mkdir()
    (material / 'claude_code+work.cred').write_text('launch-token')
    monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))
    scoped = ScopedSecrets({'claude_code'}, set(), {'claude_code': 'work'})

    assert CLAUDE.preflight_auth(MagicMock(), scoped) is None

  def test_missing_selected_instance_names_the_remedy_path(self, tmp_path, monkeypatch):
    from bro.base import credentials
    from ride.claude.harness import CLAUDE

    monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))
    scoped = ScopedSecrets({'claude_code'}, set(), {'claude_code': 'work'})

    error = CLAUDE.preflight_auth(MagicMock(), scoped)

    assert error is not None
    assert str(tmp_path / credentials.MATERIAL_DIR / 'claude_code+work.cred') in error

  def test_a_host_token_excluded_from_the_scope_fails_preflight(self, tmp_path, monkeypatch):
    from bro.base import credentials
    from ride.claude.harness import CLAUDE

    material = tmp_path / credentials.MATERIAL_DIR
    material.mkdir()
    (material / 'claude_code.cred').write_text('host-token')
    monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))

    error = CLAUDE.preflight_auth(MagicMock(), ScopedSecrets(set(), set()))

    assert error is not None
    assert 'claude_code secret not resolvable' in error


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

  def test_explicit_store_outweighs_the_launchers_ambient_selection(self, material_dir, tmp_path):
    from bro.base import credentials

    (material_dir / 'claude_code.cred').write_text('ambient-token')
    launch_store = tmp_path / 'launch'
    launch_material = launch_store / credentials.MATERIAL_DIR
    launch_material.mkdir(parents=True)
    (launch_material / 'claude_code.cred').write_text('launch-token')
    store = credentials.Store(credentials.default_registry(), launch_store, {})
    env: dict[str, str] = {}

    ride_claude_auth.apply_claude_auth(env, store=store)

    assert env == {'CLAUDE_CODE_OAUTH_TOKEN': 'launch-token'}

  def test_explicit_store_reads_its_default_pick(self, material_dir, tmp_path):
    from bro.base import credentials

    launch_store = tmp_path / 'launch'
    launch_material = launch_store / credentials.MATERIAL_DIR
    launch_material.mkdir(parents=True)
    (launch_material / 'claude_code+session.cred').write_text('launch-token')
    (launch_store / credentials.STORE_FILE).write_text(
      json.dumps({'defaults': ['claude_code+session']})
    )
    store = credentials.Store(credentials.default_registry(), launch_store, {})
    env: dict[str, str] = {}

    ride_claude_auth.apply_claude_auth(env, store=store)

    assert env == {'CLAUDE_CODE_OAUTH_TOKEN': 'launch-token'}

  def test_overwrites_inherited_stale_token(self, material_dir):
    # a CLAUDE_CODE_OAUTH_TOKEN exported by the launching shell loses to the
    # freshly resolved secret
    (material_dir / 'claude_code.cred').write_text('oauth-tok')
    env = {'CLAUDE_CODE_OAUTH_TOKEN': 'stale-tok'}
    ride_claude_auth.apply_claude_auth(env)
    assert env == {'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}
