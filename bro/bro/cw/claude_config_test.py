import json
from typing import Optional

import pytest

import bro.cw.claude_config as cw_claude_config
import bro.workspace.docker as workspace_docker
import bro.workspace.model as workspace_model
import bro.workspace.paths as workspace_paths
from bro.workspace.model import ContainerWorkspace, HostWorktree


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
    return cw_claude_config._seed_claude_json(
      claude_dir, host_file, install_method=install_method, trusted_paths=list(trusted_paths)
    )

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
    monkeypatch.setattr(cw_claude_config.Path, 'home', lambda: home)
    return home

  def _provision(self, home):
    project = home / 'project'
    worktree = project / 'var' / 'cw' / 'worktrees' / 'ws'
    return cw_claude_config._provision_host_claude_dir('ws', worktree, project), worktree

  def test_returns_the_session_claude_dir_with_seeded_json(self, home):
    claude_dir, worktree = self._provision(home)
    assert claude_dir == home / '.claude' / 'cw-sessions' / 'ws'
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
    assert settings == cw_claude_config._SESSION_SETTINGS_JSON
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
    assert settings == cw_claude_config._SESSION_SETTINGS_JSON

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

  def test_migrates_legacy_transcripts_once(self, home):
    _, worktree = self._provision(home)
    encoded = str(worktree).replace('/', '-').replace('.', '-')
    legacy = home / '.claude' / 'projects' / encoded
    legacy.mkdir(parents=True)
    (legacy / 'abc.jsonl').write_text('{"line": 1}\n')
    claude_dir, _ = self._provision(home)
    migrated = claude_dir / 'projects' / encoded / 'abc.jsonl'
    assert migrated.read_text() == '{"line": 1}\n'
    # one-shot: the legacy location is never consulted again
    (legacy / 'later.jsonl').write_text('{}\n')
    self._provision(home)
    assert not (claude_dir / 'projects' / encoded / 'later.jsonl').exists()

  def test_no_migration_when_legacy_has_no_transcripts(self, home):
    _, worktree = self._provision(home)
    encoded = str(worktree).replace('/', '-').replace('.', '-')
    (home / '.claude' / 'projects' / encoded).mkdir(parents=True)
    claude_dir, _ = self._provision(home)
    assert not (claude_dir / 'projects' / encoded).exists()

  def test_idempotent(self, home):
    first, _ = self._provision(home)
    (first / '.claude.json').write_text('{"session": "state"}')
    second, _ = self._provision(home)
    assert second == first
    assert json.loads((first / '.claude.json').read_text()) == {'session': 'state'}


class TestContainerClaudeState:
  def test_returns_the_overlay_mounts_and_claude_env(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw_claude_config.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(cw_claude_config, '_seed_claude_json', lambda d, h, **k: d / '.claude.json')
    mounts, env = cw_claude_config.container_claude_state('ws')
    claude_dir = tmp_path / '.claude' / 'cw-sessions' / 'ws'
    assert mounts == [
      f'{claude_dir / ".claude.json"}:/home/cw/.claude.json',
      f'{claude_dir}:/home/cw/.claude',
    ]
    assert env == {'DISABLE_AUTOUPDATER': '1', 'DISABLE_INSTALLATION_CHECKS': '1'}

  def test_settings_preaccept_the_bypass_permissions_dialog(self, monkeypatch, tmp_path):
    # the container workspace is an isolated clone, so --dangerously-skip-permissions
    # needs no interactive acknowledgement (container sessions only — the host
    # provision keeps the dialog, see TestProvisionHostClaudeDir)
    monkeypatch.setattr(cw_claude_config.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(cw_claude_config, '_seed_claude_json', lambda d, h, **k: d / '.claude.json')
    cw_claude_config.container_claude_state('ws')
    settings_file = tmp_path / '.claude' / 'cw-sessions' / 'ws' / 'settings.json'
    settings = json.loads(settings_file.read_text())
    assert settings['skipDangerousModePermissionPrompt'] is True


class TestPluginSeedContract:
  # the enabled plugin must also be installed: settings.json enables it (cw/claude_config.py),
  # the Dockerfile installs + stages it, and the entrypoint copies the stage into
  # the bind-mounted ~/.claude/plugins. enabling without installing is exactly the
  # regression that reintroduced the "LSP Plugin Recommendation" prompt.
  _SEED_DIR = '/opt/claude-plugins-seed'

  def test_settings_enables_pyright_lsp(self):
    assert cw_claude_config._SESSION_SETTINGS_JSON['enabledPlugins'] == {
      'pyright-lsp@claude-plugins-official': True
    }

  def test_claude_json_suppresses_marketplace_autoinstall(self):
    # the marketplace is baked into the image, so the runtime auto-install (a
    # network fetch that can also prompt) must be marked already-done.
    session_json = cw_claude_config._SESSION_CLAUDE_JSON
    assert session_json['officialMarketplaceAutoInstallAttempted'] is True

  def test_dockerfile_installs_and_stages_the_enabled_plugin(self):
    plugin = next(iter(cw_claude_config._SESSION_SETTINGS_JSON['enabledPlugins']))
    dockerfile = (workspace_docker.CONTAINER_DIR / 'Dockerfile').read_text()
    assert f'claude plugin install {plugin}' in dockerfile
    assert self._SEED_DIR in dockerfile

  def test_entrypoint_copies_the_stage(self):
    entrypoint = (workspace_docker.CONTAINER_DIR / 'entrypoint.sh').read_text()
    assert self._SEED_DIR in entrypoint
    assert '.claude/plugins' in entrypoint


class TestWorkspaceProjectsDir:
  def _worktree(self, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(workspace_paths, 'worktrees_dir', lambda project: tmp_path / 'worktrees')
    return HostWorktree('ws', tmp_path / 'project')

  def _encoded(self, worktree):
    return str(worktree.path).replace('/', '-').replace('.', '-')

  def _private(self, tmp_path, worktree):
    return (
      tmp_path / 'home' / '.claude' / 'cw-sessions' / 'ws' / 'projects' / self._encoded(worktree)
    )

  def test_container_workspace_uses_the_fixed_encoding(self, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    container = ContainerWorkspace('ws', tmp_path / 'project')
    expected = tmp_path / 'home' / '.claude' / 'cw-sessions' / 'ws' / 'projects' / '-workspace'
    assert cw_claude_config.workspace_projects_dir(container) == expected

  def test_prefers_the_private_session_projects_dir(self, monkeypatch, tmp_path):
    worktree = self._worktree(monkeypatch, tmp_path)
    private = self._private(tmp_path, worktree)
    private.mkdir(parents=True)
    assert cw_claude_config.workspace_projects_dir(worktree) == private

  def test_falls_back_to_legacy_host_projects_dir(self, monkeypatch, tmp_path):
    # sessions recorded before the private config dir live under ~/.claude/projects
    worktree = self._worktree(monkeypatch, tmp_path)
    legacy = tmp_path / 'home' / '.claude' / 'projects' / self._encoded(worktree)
    legacy.mkdir(parents=True)
    assert cw_claude_config.workspace_projects_dir(worktree) == legacy

  def test_neither_present_names_the_private_dir(self, monkeypatch, tmp_path):
    worktree = self._worktree(monkeypatch, tmp_path)
    assert cw_claude_config.workspace_projects_dir(worktree) == self._private(tmp_path, worktree)


class TestDropWorkspace:
  def _session_dir(self, tmp_path):
    session_dir = tmp_path / 'home' / '.claude' / 'cw-sessions' / 'ws'
    session_dir.mkdir(parents=True)
    return session_dir

  def test_removes_workspace_and_session_state(self, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    session_dir = self._session_dir(tmp_path)
    removed = []
    monkeypatch.setattr(
      workspace_model.ContainerWorkspace, 'remove', lambda self: removed.append(self.name)
    )
    cw_claude_config.drop_workspace(ContainerWorkspace('ws', tmp_path / 'project'))
    assert removed == ['ws']
    assert not session_dir.exists()

  def test_session_state_removed_even_when_workspace_removal_raises(self, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    session_dir = self._session_dir(tmp_path)

    def boom(self):
      raise RuntimeError('no image')

    monkeypatch.setattr(workspace_model.ContainerWorkspace, 'remove', boom)
    with pytest.raises(RuntimeError, match='no image'):
      cw_claude_config.drop_workspace(ContainerWorkspace('ws', tmp_path / 'project'))
    assert not session_dir.exists()
