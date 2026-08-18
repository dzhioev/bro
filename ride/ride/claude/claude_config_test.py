import json
from typing import Optional

import pytest

import bro.workspace.docker as workspace_docker
import ride.claude.claude_config as ride_claude_config
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace


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

  def test_missing_host_file_is_fatal(self, tmp_path):
    with pytest.raises(SystemExit):
      self._seed(_seed_dir(tmp_path), tmp_path / 'absent.json')

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
    project = home / 'project'
    workspace = home / 'state' / 'workspaces' / 'ws'
    worktree = workspace / 'tree'
    return ride_claude_config._provision_host_claude_dir(workspace, worktree, project), worktree

  def test_returns_the_session_claude_dir_with_seeded_json(self, home):
    claude_dir, worktree = self._provision(home)
    assert claude_dir == home / 'state' / 'workspaces' / 'ws' / 'claude'
    data = json.loads((claude_dir / '.claude.json').read_text())
    # the main repo root is trusted alongside the worktree: claude resolves a
    # linked worktree's trust against the repository root
    assert data['projects'] == {
      str(worktree): {'hasTrustDialogAccepted': True},
      str(home / 'project'): {'hasTrustDialogAccepted': True},
    }
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
    # only container sessions pre-accept it; on a host worktree
    # --dangerously-skip-permissions can touch the host, so the dialog stays
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
    seeded = claude_dir / 'plugins' / 'installed_plugins.json'
    assert json.loads(seeded.read_text()) == {'pyright-lsp': {}}
    assert not seeded.is_symlink()
    # first-run only: session-local plugin state is kept on later provisions
    seeded.write_text('{"session": "state"}')
    self._provision(home)
    assert json.loads(seeded.read_text()) == {'session': 'state'}

  def test_no_host_plugins_is_fine(self, home):
    claude_dir, _ = self._provision(home)
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
      'DISABLE_AUTOUPDATER': '1',
      'DISABLE_INSTALLATION_CHECKS': '1',
    }

  def test_settings_preaccept_the_bypass_permissions_dialog(self, monkeypatch, tmp_path):
    # the container workspace is an isolated clone, so --dangerously-skip-permissions
    # needs no interactive acknowledgement (container sessions only — the host
    # provision keeps the dialog, see TestProvisionHostClaudeDir)
    monkeypatch.setattr(ride_claude_config.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ride_claude_config, '_seed_claude_json', lambda d, h, **k: None)
    ride_claude_config.container_claude_state(tmp_path / 'ws')
    settings_file = tmp_path / 'ws' / 'claude' / 'settings.json'
    settings = json.loads(settings_file.read_text())
    assert settings['skipDangerousModePermissionPrompt'] is True


class TestPluginSeedContract:
  # the enabled plugin must also be installed: settings.json enables it (ride/claude_config.py),
  # the Dockerfile installs + stages it, and the entrypoint copies the stage into
  # the bind-mounted ~/.claude/plugins. enabling without installing is exactly the
  # regression that reintroduced the "LSP Plugin Recommendation" prompt.
  _SEED_DIR = '/opt/claude-plugins-seed'

  def test_settings_enables_pyright_lsp(self):
    assert ride_claude_config._SESSION_SETTINGS_JSON['enabledPlugins'] == {
      'pyright-lsp@claude-plugins-official': True
    }

  def test_claude_json_suppresses_marketplace_autoinstall(self):
    # the marketplace is baked into the image, so the runtime auto-install (a
    # network fetch that can also prompt) must be marked already-done.
    session_json = ride_claude_config._SESSION_CLAUDE_JSON
    assert session_json['officialMarketplaceAutoInstallAttempted'] is True

  def test_dockerfile_installs_and_stages_the_enabled_plugin(self):
    plugin = next(iter(ride_claude_config._SESSION_SETTINGS_JSON['enabledPlugins']))
    dockerfile = (workspace_docker.CONTAINER_DIR / 'Dockerfile').read_text()
    assert f'claude plugin install {plugin}' in dockerfile
    assert self._SEED_DIR in dockerfile

  def test_entrypoint_copies_the_stage(self):
    entrypoint = (workspace_docker.CONTAINER_DIR / 'entrypoint.sh').read_text()
    assert self._SEED_DIR in entrypoint
    assert '.claude/plugins' in entrypoint


class TestWorkspaceProjectsDir:
  def _projects(self, workspace, encoded: str):
    return workspace.path / 'claude' / 'projects' / encoded

  def test_container_workspace_uses_the_fixed_encoding(self, tmp_path):
    container = Workspace.create('ws', tmp_path / 'project', WorkspaceKind.CONTAINER)
    expected = self._projects(container, '-workspace')
    assert ride_claude_config.workspace_projects_dir(container) == expected

  def test_worktree_workspace_encodes_its_tree_path(self, tmp_path):
    worktree = Workspace.create('ws', tmp_path / 'project', WorkspaceKind.WORKTREE)
    encoded = str(worktree.tree).replace('/', '-').replace('.', '-')
    assert ride_claude_config.workspace_projects_dir(worktree) == self._projects(worktree, encoded)
