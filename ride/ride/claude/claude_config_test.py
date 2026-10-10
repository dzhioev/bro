import json
from pathlib import Path
from typing import Optional

import pytest

import ride.claude.claude_config as ride_claude_config
from ride.claude.harness import CLAUDE
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


def _host_file(tmp_path, **extra):
  path = tmp_path / 'host.json'
  path.write_text(json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'uid', **extra}))
  return path


def _seed_dir(tmp_path):
  d = tmp_path / 'seed'
  d.mkdir()
  return d


class TestSeedClaudeJSON:
  def _seed(
    self,
    claude_dir,
    host_file,
    install_method: Optional[str] = 'global',
    trusted_paths=('/workspace',),
  ):
    ride_claude_config._seed_claude_json(
      claude_dir, host_file, install_method=install_method, trusted_paths=list(trusted_paths)
    )
    return claude_dir / '.claude.json'

  def test_constructs_explicit_config_plus_identity(self, tmp_path):
    host = _host_file(tmp_path, projects={'/x': {}}, numStartups=42)
    seed = self._seed(_seed_dir(tmp_path), host)
    data = json.loads(seed.read_text())
    assert data['installMethod'] == 'global'
    assert data['hasCompletedOnboarding'] is True
    assert data['projects']['/workspace']['hasTrustDialogAccepted'] is True
    assert data['oauthAccount'] == {'id': 'acct'}
    assert data['userID'] == 'uid'
    # host machine state must not leak in
    assert '/x' not in data['projects']
    assert 'numStartups' not in data

  def test_every_trusted_path_gets_a_trust_entry(self, tmp_path):
    seed = self._seed(_seed_dir(tmp_path), _host_file(tmp_path), trusted_paths=['/w/tree', '/w'])
    assert json.loads(seed.read_text())['projects'] == {
      '/w/tree': {'hasTrustDialogAccepted': True},
      '/w': {'hasTrustDialogAccepted': True},
    }

  def test_install_method_none_carries_the_host_value(self, tmp_path):
    host = _host_file(tmp_path, installMethod='native')
    seed = self._seed(_seed_dir(tmp_path), host, install_method=None)
    assert json.loads(seed.read_text())['installMethod'] == 'native'

  def test_install_method_none_with_no_host_value_omits_the_key(self, tmp_path):
    seed = self._seed(_seed_dir(tmp_path), _host_file(tmp_path), install_method=None)
    assert 'installMethod' not in json.loads(seed.read_text())

  def test_missing_host_file_seeds_without_account_identity(self, tmp_path):
    seed = self._seed(_seed_dir(tmp_path), tmp_path / 'absent.json')
    data = json.loads(seed.read_text())
    assert data['projects']['/workspace']['hasTrustDialogAccepted'] is True
    assert 'oauthAccount' not in data
    assert 'userID' not in data

  def test_missing_identity_key_is_fatal(self, tmp_path):
    host = tmp_path / 'host.json'
    host.write_text(json.dumps({'userID': 'uid'}))
    with pytest.raises(SystemExit):
      self._seed(_seed_dir(tmp_path), host)

  def test_seed_is_not_overwritten_on_second_call(self, tmp_path):
    seed_dir = _seed_dir(tmp_path)
    seed = self._seed(seed_dir, _host_file(tmp_path))
    seed.write_text(json.dumps({'session': 'wrote-this'}))
    again = self._seed(seed_dir, _host_file(tmp_path))
    assert json.loads(again.read_text()) == {'session': 'wrote-this'}


class TestProvisionHostClaudeDir:
  @pytest.fixture
  def home(self, monkeypatch, tmp_path):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    home.joinpath('.claude.json').write_text(
      json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'uid', 'installMethod': 'native'})
    )
    monkeypatch.setattr(ride_claude_config.Path, 'home', lambda: home)
    return home

  def _provision(self, home):
    workspace = home / 'state' / 'workspaces' / 'ws'
    tree = workspace / 'tree'
    return ride_claude_config.provision_unboxed_claude_dir(workspace, tree), tree

  def test_returns_the_session_claude_dir_with_seeded_json(self, home):
    claude_dir, worktree = self._provision(home)
    assert claude_dir == home / 'state' / 'workspaces' / 'ws' / 'claude'
    data = json.loads((claude_dir / '.claude.json').read_text())
    assert data['projects'] == {str(worktree): {'hasTrustDialogAccepted': True}}
    assert data['installMethod'] == 'native'

  def test_writes_the_session_settings_leaving_host_state_out(self, home):
    host_claude = home / '.claude'
    (host_claude / 'settings.json').write_text('{"permissions": {"allow": ["Bash(*)"]}}')
    (host_claude / '.credentials.json').write_text('secret')
    claude_dir, _ = self._provision(home)
    settings = json.loads((claude_dir / 'settings.json').read_text())
    assert settings == ride_claude_config._SESSION_SETTINGS_JSON
    assert not (claude_dir / '.credentials.json').exists()
    assert not (claude_dir / 'CLAUDE.md').exists()

  def test_settings_do_not_preaccept_the_bypass_permissions_dialog(self, home):
    # only boxed sessions pre-accept it; an unboxed session can touch the
    # launcher's filesystem, so the dialog stays
    claude_dir, _ = self._provision(home)
    settings = json.loads((claude_dir / 'settings.json').read_text())
    assert 'skipDangerousModePermissionPrompt' not in settings

  def test_settings_rewritten_each_provision(self, home):
    claude_dir, _ = self._provision(home)
    (claude_dir / 'settings.json').write_text('{"stale": true}')
    self._provision(home)
    settings = json.loads((claude_dir / 'settings.json').read_text())
    assert settings == ride_claude_config._SESSION_SETTINGS_JSON

  def test_seeds_host_plugins_once(self, home):
    host_plugins = home / '.claude' / 'plugins'
    (host_plugins / 'marketplaces').mkdir(parents=True)
    (host_plugins / 'installed_plugins.json').write_text('{"pyright-lsp": {}}')
    claude_dir, _ = self._provision(home)
    ride_claude_config.seed_session_plugins(claude_dir, container=False)
    seeded = claude_dir / 'plugins' / 'installed_plugins.json'
    assert json.loads(seeded.read_text()) == {'pyright-lsp': {}}
    assert not seeded.is_symlink()
    # first-run only: session-local plugin state is kept on later provisions
    seeded.write_text('{"session": "state"}')
    ride_claude_config.seed_session_plugins(claude_dir, container=False)
    assert json.loads(seeded.read_text()) == {'session': 'state'}

  def test_no_host_plugins_is_fine(self, home):
    claude_dir, _ = self._provision(home)
    ride_claude_config.seed_session_plugins(claude_dir, container=False)
    assert not (claude_dir / 'plugins').exists()

  def test_idempotent(self, home):
    first, _ = self._provision(home)
    (first / '.claude.json').write_text('{"session": "state"}')
    second, _ = self._provision(home)
    assert second == first
    assert json.loads((first / '.claude.json').read_text()) == {'session': 'state'}


class TestContainerClaudeState:
  def test_returns_the_state_mount_and_claude_env(self, monkeypatch, tmp_path):
    monkeypatch.setattr(ride_claude_config.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ride_claude_config, '_seed_claude_json', lambda d, h, **k: None)
    mounts, env = ride_claude_config.container_claude_state(tmp_path / 'ws')
    claude_dir = tmp_path / 'ws' / 'claude'
    assert mounts == [f'{claude_dir}:/home/ride/.claude']
    assert env == {
      'CLAUDE_CONFIG_DIR': '/home/ride/.claude',
      'DISABLE_INSTALLATION_CHECKS': '1',
    }

  def test_settings_preaccept_the_bypass_permissions_dialog(self, monkeypatch, tmp_path):
    # the boxed workspace is an isolated clone, so --dangerously-skip-permissions
    # needs no interactive acknowledgement (container sessions only — the host
    # provision keeps the dialog, see TestProvisionHostClaudeDir)
    monkeypatch.setattr(ride_claude_config.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ride_claude_config, '_seed_claude_json', lambda d, h, **k: None)
    ride_claude_config.container_claude_state(tmp_path / 'ws')
    settings_file = tmp_path / 'ws' / 'claude' / 'settings.json'
    settings = json.loads(settings_file.read_text())
    assert settings['skipDangerousModePermissionPrompt'] is True


class TestPluginSeedContract:
  def test_dockerfile_installs_and_stages_the_enabled_plugin(self):
    plugin = next(iter(ride_claude_config._SESSION_SETTINGS_JSON['enabledPlugins']))
    dockerfile = CLAUDE.runtime_image().dockerfile
    assert f'claude plugin install {plugin}' in dockerfile
    assert str(ride_claude_config._CONTAINER_PLUGIN_SEED) in dockerfile

  def test_session_seed_copies_the_container_stage_once(self, monkeypatch, tmp_path):
    source = tmp_path / 'seed'
    source.mkdir()
    (source / 'installed_plugins.json').write_text('{"pyright-lsp": {}}')
    destination = tmp_path / 'claude'
    monkeypatch.setattr(ride_claude_config, '_CONTAINER_PLUGIN_SEED', source)

    ride_claude_config.seed_session_plugins(destination, container=True)
    installed = destination / 'plugins' / 'installed_plugins.json'
    assert json.loads(installed.read_text()) == {'pyright-lsp': {}}
    installed.write_text('{"session": {}}')
    ride_claude_config.seed_session_plugins(destination, container=True)
    assert json.loads(installed.read_text()) == {'session': {}}


class TestWorkspaceProjectsDir:
  def _projects(self, workspace, encoded: str):
    return workspace.path / 'claude' / 'projects' / encoded

  def test_container_workspace_uses_the_fixed_encoding(self, tmp_path):
    container = Workspace.create('ws', tmp_path / 'project', Isolation.BOXED)
    expected = self._projects(container, '-workspace')
    assert ride_claude_config.workspace_projects_dir(container) == expected

  def test_worktree_workspace_encodes_its_tree_path(self, tmp_path):
    worktree = Workspace.create('ws', tmp_path / 'project', Isolation.UNBOXED)
    assert ride_claude_config.workspace_projects_dir(worktree) == self._projects(
      worktree, ride_claude_config.encode_project_path(worktree.tree)
    )


class TestClaudeConfigDir:
  def test_names_the_dir_the_session_declares(self, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'w' / 'claude'))
    assert ride_claude_config.claude_config_dir() == tmp_path / 'w' / 'claude'

  def test_outside_a_claude_session_there_is_no_config_dir(self, monkeypatch):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    with pytest.raises(RuntimeError, match='CLAUDE_CONFIG_DIR'):
      ride_claude_config.claude_config_dir()


class TestProjectsDir:
  def test_encodes_slashes_and_dots(self):
    assert (
      ride_claude_config.encode_project_path(Path('/home/u/project/.claude/wt'))
      == '-home-u-project--claude-wt'
    )

  def test_container_clone_encodes_to_the_mount_point(self):
    assert ride_claude_config.encode_project_path(Path('/workspace')) == '-workspace'

  # the expected names below are the directories the pinned Claude Code created
  # for these paths (`project_dir_llm_test.py` holds the rule live)
  def test_encodes_every_character_outside_ascii_letters_and_digits(self):
    assert (
      ride_claude_config.encode_project_path(Path('/tmp/p/home/john_doe/my project'))
      == '-tmp-p-home-john-doe-my-project'
    )

  def test_encodes_each_utf16_unit_of_a_character_beyond_the_basic_plane(self):
    assert ride_claude_config.encode_project_path(Path('/tmp/café-\U0001d11e')) == '-tmp-caf----'

  def test_cuts_a_long_name_and_suffixes_the_hash_of_the_whole_path(self):
    long = Path('/tmp/p') / f'long-{"segment" * 30}'
    assert (
      ride_claude_config.encode_project_path(long) == f'-tmp-p-long-{"segment" * 26}segmen-k5skbv'
    )

  def test_projects_dir_sits_under_the_active_config_root(self, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'session'))
    assert ride_claude_config.claude_projects_dir(Path('/workspace')) == (
      tmp_path / 'session' / 'projects' / '-workspace'
    )
