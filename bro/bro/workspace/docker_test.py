import signal

import pytest

import workspace.docker
import workspace.project


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

    monkeypatch.setattr(workspace.docker.subprocess, 'run', fake_run)
    return calls

  def test_creates_then_injects_store(self, monkeypatch):
    def results(argv):
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, results)
    container_id = workspace.docker._create_container(
      ['docker', 'create', 'ARGS'], b'TARBALL', 'ws'
    )
    assert container_id == 'cid123'
    cp = next(c for c in calls if c['argv'][:3] == ['docker', 'cp', '-'])
    assert cp['argv'][3] == 'cid123:/home/cw'
    assert cp['input'] == b'TARBALL'

  def test_create_failure_raises(self, monkeypatch):
    self._patch_run(monkeypatch, lambda argv: _FakeProc(returncode=1, stderr='boom'))
    with pytest.raises(RuntimeError, match='docker create'):
      workspace.docker._create_container(['docker', 'create'], b'', 'ws')

  def test_cp_failure_removes_container_and_raises(self, monkeypatch):
    def results(argv):
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:3] == ['docker', 'cp', '-']:
        return _FakeProc(returncode=1, stderr=b'no such container')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, results)
    with pytest.raises(RuntimeError, match='docker cp'):
      workspace.docker._create_container(['docker', 'create'], b'', 'ws')
    assert calls[-1]['argv'] == ['docker', 'rm', '-f', 'cid123']


class TestContainerRunning:
  def _probe(self, monkeypatch, result: _FakeProc):
    calls: list = []

    def fake_run(argv, *a, **k):
      calls.append(argv)
      return result

    monkeypatch.setattr(workspace.docker.subprocess, 'run', fake_run)
    return calls

  def test_running(self, monkeypatch):
    calls = self._probe(monkeypatch, _FakeProc(returncode=0, stdout='true\n'))
    assert workspace.docker.container_running('cid123') is True
    assert calls == [['docker', 'inspect', '--format', '{{.State.Running}}', 'cid123']]

  def test_exited(self, monkeypatch):
    self._probe(monkeypatch, _FakeProc(returncode=0, stdout='false\n'))
    assert workspace.docker.container_running('cid123') is False

  def test_removed_reads_as_not_running(self, monkeypatch):
    # --rm removes an exited container, so the inspect error is the common exit shape
    self._probe(monkeypatch, _FakeProc(returncode=1, stderr='no such object'))
    assert workspace.docker.container_running('cid123') is False


class TestSuspendUntilContinued:
  def _dance(self, monkeypatch, pause_result=None):
    events: list = []

    def fake_run(argv, *a, **k):
      events.append(argv)
      if pause_result is not None and argv[1] == 'pause':
        return pause_result
      return _FakeProc(returncode=0)

    monkeypatch.setattr(workspace.docker.subprocess, 'run', fake_run)
    monkeypatch.setattr(
      workspace.docker.os, 'kill', lambda pid, sig: events.append(('kill', pid, sig))
    )
    return events

  def test_freezes_stops_own_group_then_thaws(self, monkeypatch):
    events = self._dance(monkeypatch)
    workspace.docker.suspend_until_continued('cid123')
    # the stop targets the whole process group (pid 0): the launching shell reports its
    # job stopped only when every process in it stops, dive-in wrappers included
    assert events == [
      ['docker', 'pause', 'cid123'],
      ('kill', 0, signal.SIGTSTP),
      ['docker', 'unpause', 'cid123'],
    ]

  def test_freezer_failure_warns_but_still_suspends(self, monkeypatch, caplog):
    events = self._dance(monkeypatch, pause_result=_FakeProc(returncode=1, stderr='not running'))
    workspace.docker.suspend_until_continued('cid123')
    assert ('kill', 0, signal.SIGTSTP) in events
    assert events[-1] == ['docker', 'unpause', 'cid123']
    assert 'docker pause cid123 failed: not running' in caplog.text


class TestImageTag:
  def test_repository_and_submodule_manifest_come_from_the_project(self, monkeypatch, tmp_path):
    (tmp_path / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\nimage-repository = "custom-images"\n'
    )
    (tmp_path / 'uv.lock').write_text('lock')
    monkeypatch.setattr(workspace.docker, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(workspace.project, 'project_root', lambda: tmp_path)
    without_submodule = workspace.docker.image_tag()
    assert without_submodule.startswith('custom-images:')
    (tmp_path / 'ppp').mkdir()
    (tmp_path / 'ppp' / 'pyproject.toml').write_text('[project.scripts]')
    with_submodule = workspace.docker.image_tag()
    assert with_submodule.startswith('custom-images:')
    assert with_submodule != without_submodule


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

    monkeypatch.setattr(workspace.docker.subprocess, 'run', fake_run)
    return calls

  def test_removes_all_but_current_smoke_test_and_untagged(self, monkeypatch):
    listing = _FakeProc(
      returncode=0,
      stdout='bro/ppp-dev:cur\nbro/ppp-dev:smoke-test\nbro/ppp-dev:<none>\nbro/ppp-dev:old1\nbro/ppp-dev:old2\n',
    )
    calls = self._patch_run(monkeypatch, listing)
    workspace.docker._prune_superseded_images('bro/ppp-dev:cur')
    removals = [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']]
    assert removals == [
      ['docker', 'image', 'rm', 'bro/ppp-dev:old1'],
      ['docker', 'image', 'rm', 'bro/ppp-dev:old2'],
    ]

  def test_refused_removal_is_tolerated(self, monkeypatch):
    # `docker image rm` without -f refuses images a container still references;
    # that refusal keeps live sessions' images and must not abort the prune
    listing = _FakeProc(
      returncode=0, stdout='bro/ppp-dev:cur\nbro/ppp-dev:in-use\nbro/ppp-dev:old\n'
    )

    def remove_result(argv):
      if argv[-1] == 'bro/ppp-dev:in-use':
        return _FakeProc(returncode=1, stderr='image is being used')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, listing, remove_result)
    workspace.docker._prune_superseded_images('bro/ppp-dev:cur')
    removals = [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']]
    assert removals == [
      ['docker', 'image', 'rm', 'bro/ppp-dev:in-use'],
      ['docker', 'image', 'rm', 'bro/ppp-dev:old'],
    ]

  def test_listing_failure_skips_pruning(self, monkeypatch):
    calls = self._patch_run(monkeypatch, _FakeProc(returncode=1, stderr='daemon down'))
    workspace.docker._prune_superseded_images('bro/ppp-dev:cur')
    assert [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']] == []


class TestPrepareContainer:
  def test_runs_the_shared_prepare_sequence_from_the_launch(self, monkeypatch, tmp_path):
    project = tmp_path / 'project'
    events: list = []
    monkeypatch.setattr(workspace.docker, 'image_tag', lambda: events.append('tag') or 'image')
    monkeypatch.setattr(
      workspace.docker, '_ensure_image', lambda tag: events.append(('ensure', tag))
    )
    monkeypatch.setattr(
      workspace.docker.credentials,
      'build_scoped_store',
      lambda secrets, optional=(): events.append(('store', secrets, optional)) or {'x': b'y'},
    )
    monkeypatch.setattr(workspace.docker, '_bro_tarball', lambda store: b'TARBALL')
    monkeypatch.setattr(
      workspace.docker,
      '_docker_create_argv',
      lambda *args, **kwargs: events.append(('argv', args, kwargs)) or ['docker', 'create'],
    )
    monkeypatch.setattr(
      workspace.docker,
      '_create_container',
      lambda argv, tarball, name: events.append(('create', argv, tarball, name)) or 'cid',
    )
    launch = workspace.docker.Launch(
      name='ws',
      command=['claude'],
      env={'MARKER': 'x'},
      secrets=('github',),
      docker_sock=False,
      tty=False,
      forward_env=False,
      optional_secrets=('openai',),
      extra_mounts=('/host:/container',),
    )
    assert workspace.docker.prepare_container(launch, project) == 'cid'
    assert (project / 'var' / 'cw' / 'containers' / 'ws').is_dir()
    assert events[0:3] == [
      'tag',
      ('ensure', 'image'),
      ('store', ('github',), ('openai',)),
    ]
    argv_event = events[3]
    assert argv_event[0] == 'argv'
    assert argv_event[1] == (
      'image',
      'ws',
      project,
      project / 'var' / 'cw' / 'containers' / 'ws',
      ['claude'],
    )
    assert argv_event[2] == {
      'docker_sock': False,
      'extra_env': {'MARKER': 'x'},
      'forward_env': False,
      'tty': False,
      'extra_mounts': ['/host:/container'],
    }
    assert events[4] == ('create', ['docker', 'create'], b'TARBALL', 'ws')


class TestDockerCreateArgv:
  @pytest.fixture
  def build_argv(self, monkeypatch, tmp_path):
    monkeypatch.setattr(workspace.docker.Path, 'home', lambda: tmp_path)

    def build(**kwargs):
      return workspace.docker._docker_create_argv(
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

  def test_no_bro_mount(self, build_argv):
    # the scoped store is injected via `docker cp`, never bind-mounted
    assert not any('/home/cw/.bro' in a for a in build_argv())

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

  def test_ambient_cw_bro_never_forwarded(self, build_argv, monkeypatch):
    # every container runs its own bro, set explicitly via extra_env by the
    # launch surface — the caller's ambient CW_BRO must not leak in and
    # mis-theme it.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' not in build_argv()
    assert 'CW_BRO=pm' in build_argv(extra_env={'CW_BRO': 'pm'})

  def test_forward_env_false_switches_the_forward_loop_off(self, build_argv, monkeypatch):
    # a broker-spawned child's environment is its LaunchSpec snapshot (extra_env)
    # only; none of the ambient _DOCKER_FORWARD_ENV vars may reach it
    for var in workspace.docker._DOCKER_FORWARD_ENV:
      monkeypatch.setenv(var, 'ambient')
    argv = build_argv(forward_env=False, extra_env={'MARKER': 'x'})
    assert not any(var in argv for var in workspace.docker._DOCKER_FORWARD_ENV)
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
