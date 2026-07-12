import json

import pytest

import cw.claude_config
import cw.docker


class TestPluginSeedContract:
  # the enabled plugin must also be installed: settings.json enables it (cw/docker.py),
  # the Dockerfile installs + stages it, and the entrypoint copies the stage into
  # the bind-mounted ~/.claude/plugins. enabling without installing is exactly the
  # regression that reintroduced the "LSP Plugin Recommendation" prompt.
  _SEED_DIR = '/opt/claude-plugins-seed'

  def test_settings_enables_pyright_lsp(self):
    assert cw.claude_config._SESSION_SETTINGS_JSON['enabledPlugins'] == {
      'pyright-lsp@claude-plugins-official': True
    }

  def test_claude_json_suppresses_marketplace_autoinstall(self):
    # the marketplace is baked into the image, so the runtime auto-install (a
    # network fetch that can also prompt) must be marked already-done.
    session_json = cw.claude_config._SESSION_CLAUDE_JSON
    assert session_json['officialMarketplaceAutoInstallAttempted'] is True

  def test_dockerfile_installs_and_stages_the_enabled_plugin(self):
    plugin = next(iter(cw.claude_config._SESSION_SETTINGS_JSON['enabledPlugins']))
    dockerfile = (cw.docker.CONTAINER_DIR / 'Dockerfile').read_text()
    assert f'claude plugin install {plugin}' in dockerfile
    assert self._SEED_DIR in dockerfile

  def test_entrypoint_copies_the_stage(self):
    entrypoint = (cw.docker.CONTAINER_DIR / 'entrypoint.sh').read_text()
    assert self._SEED_DIR in entrypoint
    assert '.claude/plugins' in entrypoint


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestCreateContainer:
  def _patch_run(self, monkeypatch, results):
    calls: list = []

    def fake_run(argv, *a, **k):
      calls.append({'argv': argv, 'input': k.get('input')})
      return results(argv)

    monkeypatch.setattr(cw.docker.subprocess, 'run', fake_run)
    return calls

  def test_creates_then_injects_store(self, monkeypatch):
    def results(argv):
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, results)
    container_id = cw.docker._create_container(['docker', 'create', 'ARGS'], b'TARBALL', 'ws')
    assert container_id == 'cid123'
    cp = next(c for c in calls if c['argv'][:3] == ['docker', 'cp', '-'])
    assert cp['argv'][3] == 'cid123:/home/cw'
    assert cp['input'] == b'TARBALL'

  def test_create_failure_raises(self, monkeypatch):
    self._patch_run(monkeypatch, lambda argv: _FakeProc(returncode=1, stderr='boom'))
    with pytest.raises(RuntimeError, match='docker create'):
      cw.docker._create_container(['docker', 'create'], b'', 'ws')

  def test_cp_failure_removes_container_and_raises(self, monkeypatch):
    def results(argv):
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:3] == ['docker', 'cp', '-']:
        return _FakeProc(returncode=1, stderr=b'no such container')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, results)
    with pytest.raises(RuntimeError, match='docker cp'):
      cw.docker._create_container(['docker', 'create'], b'', 'ws')
    assert calls[-1]['argv'] == ['docker', 'rm', '-f', 'cid123']


class TestPruneSupersededImages:
  def _patch_run(self, monkeypatch, listing: _FakeProc, remove_result=None):
    calls: list = []

    def fake_run(argv, *a, **k):
      calls.append(argv)
      if argv[:2] == ['docker', 'images']:
        return listing
      if remove_result is not None:
        return remove_result(argv)
      return _FakeProc(returncode=0)

    monkeypatch.setattr(cw.docker.subprocess, 'run', fake_run)
    return calls

  def test_removes_all_but_current_smoke_test_and_untagged(self, monkeypatch):
    listing = _FakeProc(
      returncode=0,
      stdout='ppp-cw:cur\nppp-cw:smoke-test\nppp-cw:<none>\nppp-cw:old1\nppp-cw:old2\n',
    )
    calls = self._patch_run(monkeypatch, listing)
    cw.docker._prune_superseded_images('ppp-cw:cur')
    removals = [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']]
    assert removals == [
      ['docker', 'image', 'rm', 'ppp-cw:old1'],
      ['docker', 'image', 'rm', 'ppp-cw:old2'],
    ]

  def test_refused_removal_is_tolerated(self, monkeypatch):
    # `docker image rm` without -f refuses images a container still references;
    # that refusal keeps live sessions' images and must not abort the prune
    listing = _FakeProc(returncode=0, stdout='ppp-cw:cur\nppp-cw:in-use\nppp-cw:old\n')

    def remove_result(argv):
      if argv[-1] == 'ppp-cw:in-use':
        return _FakeProc(returncode=1, stderr='image is being used')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, listing, remove_result)
    cw.docker._prune_superseded_images('ppp-cw:cur')
    removals = [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']]
    assert removals == [
      ['docker', 'image', 'rm', 'ppp-cw:in-use'],
      ['docker', 'image', 'rm', 'ppp-cw:old'],
    ]

  def test_listing_failure_skips_pruning(self, monkeypatch):
    calls = self._patch_run(monkeypatch, _FakeProc(returncode=1, stderr='daemon down'))
    cw.docker._prune_superseded_images('ppp-cw:cur')
    assert [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']] == []


class TestDockerCreateArgv:
  @pytest.fixture
  def build_argv(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.docker.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(cw.docker, '_seed_claude_json', lambda d, h, **k: tmp_path / '.claude.json')

    def build(**kwargs):
      return cw.docker._docker_create_argv(
        'tag', 'ws', tmp_path / 'proj', tmp_path / 'sess', ['claude'], **kwargs
      )

    return build

  def test_uses_docker_create_run_equivalent(self, build_argv):
    # `docker create -it --rm` is the unstarted half of `docker run -it --rm`;
    # run_in_container pairs it with `docker start -a -i`.
    assert build_argv()[:4] == ['docker', 'create', '-it', '--rm']

  def test_settings_preaccept_the_bypass_permissions_dialog(self, build_argv, tmp_path):
    # the container workspace is an isolated clone, so --dangerously-skip-permissions
    # needs no interactive acknowledgement (container sessions only — the host
    # provision keeps the dialog, see claude_config_test)
    build_argv()
    settings_file = tmp_path / '.claude' / 'cw-sessions' / 'ws' / 'settings.json'
    settings = json.loads(settings_file.read_text())
    assert settings['skipDangerousModePermissionPrompt'] is True

  def test_docker_sock_mounted_by_default(self, build_argv):
    assert '/var/run/docker.sock:/var/run/docker.sock' in build_argv()

  def test_docker_sock_dropped_when_disabled(self, build_argv):
    assert '/var/run/docker.sock:/var/run/docker.sock' not in build_argv(docker_sock=False)

  def test_base_ref_passed_as_env(self, build_argv):
    argv = build_argv(extra_env={'CW_BASE_REF': 'deadbeef'})
    assert 'CW_BASE_REF=deadbeef' in argv

  def test_no_ppp_mount(self, build_argv):
    # the scoped store is injected via `docker cp`, never bind-mounted
    assert not any('/home/cw/.ppp' in a for a in build_argv())

  def test_no_out_of_band_github_token_mount(self, build_argv):
    assert not any('/run/secrets/github_token' in a for a in build_argv())

  def test_ambient_github_token_not_forwarded(self, build_argv, monkeypatch):
    # an ambient host GITHUB_TOKEN must not leak into the container — github
    # arrives only via the scoped `github` secret's install hook.
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_leak')
    argv = build_argv()
    assert 'GITHUB_TOKEN' not in argv
    assert not any('ghp_leak' in a for a in argv)

  def test_term_forwarded_for_color_fidelity(self, build_argv, monkeypatch):
    # forward the host TERM so in-container TUIs detect the same color tier — docker
    # otherwise defaults TERM=xterm (low tier), flattening dim/256-color styling.
    monkeypatch.setenv('TERM', 'tmux-256color')
    argv = build_argv()
    assert 'TERM' in argv
    assert argv[argv.index('TERM') - 1] == '-e'

  def test_cw_bro_forwarded_by_default(self, build_argv, monkeypatch):
    # the in-place runner reads CW_BRO to theme the session and surface skills,
    # so a themed native session needs it to reach the container.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' in build_argv()

  def test_cw_bro_dropped_when_forward_bro_false(self, build_argv, monkeypatch):
    # the ask/do/call hop runs its own named bro, so the calling session's
    # ambient CW_BRO must not leak in and mis-theme it.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' not in build_argv(forward_bro=False)

  def test_forward_env_false_switches_the_forward_loop_off(self, build_argv, monkeypatch):
    # a broker-spawned child's environment is its LaunchSpec snapshot (extra_env)
    # only; none of the ambient _DOCKER_FORWARD_ENV vars may reach it
    for var in cw.docker._DOCKER_FORWARD_ENV:
      monkeypatch.setenv(var, 'ambient')
    argv = build_argv(forward_env=False, extra_env={'MARKER': 'x'})
    assert not any(var in argv for var in cw.docker._DOCKER_FORWARD_ENV)
    assert 'MARKER=x' in argv

  def test_extra_env_injected_as_explicit_key_value(self, build_argv, monkeypatch):
    # extra_env sets the value here (`-e KEY=VALUE`), unlike _DOCKER_FORWARD_ENV which
    # forwards a host var by name — so it works even with no such var on the host.
    monkeypatch.delenv('TRAILS_DISABLED', raising=False)
    argv = build_argv(extra_env={'TRAILS_DISABLED': '1'})
    assert 'TRAILS_DISABLED=1' in argv
    assert argv[argv.index('TRAILS_DISABLED=1') - 1] == '-e'

  def test_no_extra_env_by_default(self, build_argv):
    assert not any('TRAILS_DISABLED' in a for a in build_argv())

  def test_tty_dropped_when_disabled(self, build_argv):
    # the broker's non-TTY child variant: no -it, so stdout/stderr stay separate.
    argv = build_argv(tty=False)
    assert '-it' not in argv
    assert argv[:2] == ['docker', 'create']

  def test_extra_mounts_added_as_volumes(self, build_argv):
    argv = build_argv(extra_mounts=['/h/s.sock:/run/broker.sock'])
    assert '/h/s.sock:/run/broker.sock' in argv
    assert argv[argv.index('/h/s.sock:/run/broker.sock') - 1] == '-v'

  def test_no_extra_mounts_by_default(self, build_argv):
    # the every-session path stays unchanged: still -it, no stray broker mount.
    assert build_argv()[:4] == ['docker', 'create', '-it', '--rm']
