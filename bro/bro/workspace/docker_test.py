import pytest

import cw.containers
import cw.docker


class TestPluginSeedContract:
  # the enabled plugin must also be installed: settings.json enables it (cw/docker.py),
  # the Dockerfile installs + stages it, and the entrypoint copies the stage into
  # the bind-mounted ~/.claude/plugins. enabling without installing is exactly the
  # regression that reintroduced the "LSP Plugin Recommendation" prompt.
  _SEED_DIR = '/opt/claude-plugins-seed'

  def test_settings_enables_pyright_lsp(self):
    assert cw.docker._CONTAINER_SETTINGS_JSON['enabledPlugins'] == {
      'pyright-lsp@claude-plugins-official': True
    }

  def test_claude_json_suppresses_marketplace_autoinstall(self):
    # the marketplace is baked into the image, so the runtime auto-install (a
    # network fetch that can also prompt) must be marked already-done.
    assert cw.containers._CONTAINER_CLAUDE_JSON['officialMarketplaceAutoInstallAttempted'] is True

  def test_dockerfile_installs_and_stages_the_enabled_plugin(self):
    plugin = next(iter(cw.docker._CONTAINER_SETTINGS_JSON['enabledPlugins']))
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
    cid = cw.docker._create_container(['docker', 'create', 'ARGS'], b'TARBALL', 'ws')
    assert cid == 'cid123'
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


class TestDockerCreateArgv:
  @pytest.fixture
  def build_argv(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.docker.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(
      cw.containers, '_seed_container_claude_json', lambda d, h: tmp_path / '.claude.json'
    )

    def build(**kwargs):
      return cw.docker._docker_create_argv(
        'tag', 'ws', tmp_path / 'proj', tmp_path / 'sess', ['claude'], **kwargs
      )

    return build

  def test_uses_docker_create_run_equivalent(self, build_argv):
    # `docker create -it --rm` is the unstarted half of `docker run -it --rm`;
    # run_in_container pairs it with `docker start -a -i`.
    assert build_argv()[:4] == ['docker', 'create', '-it', '--rm']

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
    # the Claude Code session path relies on CW_BRO reaching the container so the
    # entrypoint runs `cw populate-bro-skills`.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' in build_argv()

  def test_cw_bro_dropped_when_forward_bro_false(self, build_argv, monkeypatch):
    # the ask/do/call hop runs the bro as an LLM process (no Claude Code), so the
    # calling session's ambient CW_BRO must not leak in and trigger a skills populate.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' not in build_argv(forward_bro=False)

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
