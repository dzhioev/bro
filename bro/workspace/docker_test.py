import signal

import pytest

import bro.workspace.docker as workspace_docker
import bro.workspace.project as workspace_project
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace


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

    monkeypatch.setattr(workspace_docker.subprocess, 'run', fake_run)
    return calls

  def test_creates_then_injects_store(self, monkeypatch):
    def results(argv):
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, results)
    container_id = workspace_docker._create_container(
      ['docker', 'create', 'ARGS'], b'TARBALL', 'ws'
    )
    assert container_id == 'cid123'
    cp = next(c for c in calls if c['argv'][:3] == ['docker', 'cp', '-'])
    assert cp['argv'][3] == 'cid123:/home/cw'
    assert cp['input'] == b'TARBALL'

  def test_create_failure_raises(self, monkeypatch):
    self._patch_run(monkeypatch, lambda argv: _FakeProc(returncode=1, stderr='boom'))
    with pytest.raises(RuntimeError, match='docker create'):
      workspace_docker._create_container(['docker', 'create'], b'', 'ws')

  def test_cp_failure_removes_container_and_raises(self, monkeypatch):
    def results(argv):
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:3] == ['docker', 'cp', '-']:
        return _FakeProc(returncode=1, stderr=b'no such container')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, results)
    with pytest.raises(RuntimeError, match='docker cp'):
      workspace_docker._create_container(['docker', 'create'], b'', 'ws')
    assert calls[-1]['argv'] == ['docker', 'rm', '-f', 'cid123']


class TestContainerRunning:
  def _probe(self, monkeypatch, result: _FakeProc):
    calls: list = []

    def fake_run(argv, *a, **k):
      calls.append(argv)
      return result

    monkeypatch.setattr(workspace_docker.subprocess, 'run', fake_run)
    return calls

  def test_running(self, monkeypatch):
    calls = self._probe(monkeypatch, _FakeProc(returncode=0, stdout='true\n'))
    assert workspace_docker.container_running('cid123') is True
    assert calls == [['docker', 'inspect', '--format', '{{.State.Running}}', 'cid123']]

  def test_exited(self, monkeypatch):
    self._probe(monkeypatch, _FakeProc(returncode=0, stdout='false\n'))
    assert workspace_docker.container_running('cid123') is False

  def test_removed_reads_as_not_running(self, monkeypatch):
    # --rm removes an exited container, so the inspect error is the common exit shape
    self._probe(monkeypatch, _FakeProc(returncode=1, stderr='no such object'))
    assert workspace_docker.container_running('cid123') is False


class TestSuspendUntilContinued:
  def _dance(self, monkeypatch, pause_result=None):
    events: list = []

    def fake_run(argv, *a, **k):
      events.append(argv)
      if pause_result is not None and argv[1] == 'pause':
        return pause_result
      return _FakeProc(returncode=0)

    monkeypatch.setattr(workspace_docker.subprocess, 'run', fake_run)
    monkeypatch.setattr(
      workspace_docker.os, 'kill', lambda pid, sig: events.append(('kill', pid, sig))
    )
    return events

  def test_freezes_stops_own_group_then_thaws(self, monkeypatch):
    events = self._dance(monkeypatch)
    workspace_docker.suspend_until_continued('cid123')
    # the stop targets the whole process group (pid 0): the launching shell reports its
    # job stopped only when every process in it stops, dive-in wrappers included
    assert events == [
      ['docker', 'pause', 'cid123'],
      ('kill', 0, signal.SIGTSTP),
      ['docker', 'unpause', 'cid123'],
    ]

  def test_freezer_failure_warns_but_still_suspends(self, monkeypatch, caplog):
    events = self._dance(monkeypatch, pause_result=_FakeProc(returncode=1, stderr='not running'))
    workspace_docker.suspend_until_continued('cid123')
    assert ('kill', 0, signal.SIGTSTP) in events
    assert events[-1] == ['docker', 'unpause', 'cid123']
    assert 'docker pause cid123 failed: not running' in caplog.text


class TestImageTag:
  @pytest.fixture
  def project(self, monkeypatch, tmp_path):
    (tmp_path / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "foo"\nimage-repository = "custom-images"\n\n'
      '[tool.uv.workspace]\nmembers = ["member"]\n'
    )
    (tmp_path / 'uv.lock').write_text('lock')
    (tmp_path / 'member').mkdir()
    (tmp_path / 'member' / 'pyproject.toml').write_text('[project]\nname = "member"\n')
    monkeypatch.setattr(workspace_docker, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    return tmp_path

  def test_repository_comes_from_the_project(self, project):
    assert workspace_docker.image_tag().startswith('custom-images:')

  def test_a_member_manifest_edit_changes_the_tag(self, project):
    before = workspace_docker.image_tag()
    (project / 'member' / 'pyproject.toml').write_text(
      '[project]\nname = "member"\nversion = "2"\n'
    )
    assert workspace_docker.image_tag() != before

  def test_a_framework_shell_helper_edit_changes_the_tag(self, project, monkeypatch, tmp_path):
    before = workspace_docker.image_tag()
    setup = tmp_path / 'framework-setup'
    setup.mkdir()
    for name in workspace_docker.build_context.SHELL_HELPERS:
      (setup / name).write_text(f'# {name} edited\n')
    monkeypatch.setattr(workspace_docker.build_context, 'SETUP_DIR', setup)
    assert workspace_docker.image_tag() != before


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

    monkeypatch.setattr(workspace_docker.subprocess, 'run', fake_run)
    return calls

  def test_removes_all_but_current_smoke_test_and_untagged(self, monkeypatch):
    listing = _FakeProc(
      returncode=0,
      stdout='bro/framework:cur\nbro/framework:smoke-test\nbro/framework:<none>\nbro/framework:old1\nbro/framework:old2\n',
    )
    calls = self._patch_run(monkeypatch, listing)
    workspace_docker._prune_superseded_images('bro/framework:cur')
    removals = [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']]
    assert removals == [
      ['docker', 'image', 'rm', 'bro/framework:old1'],
      ['docker', 'image', 'rm', 'bro/framework:old2'],
    ]

  def test_refused_removal_is_tolerated(self, monkeypatch):
    # `docker image rm` without -f refuses images a container still references;
    # that refusal keeps live sessions' images and must not abort the prune
    listing = _FakeProc(
      returncode=0, stdout='bro/framework:cur\nbro/framework:in-use\nbro/framework:old\n'
    )

    def remove_result(argv):
      if argv[-1] == 'bro/framework:in-use':
        return _FakeProc(returncode=1, stderr='image is being used')
      return _FakeProc(returncode=0)

    calls = self._patch_run(monkeypatch, listing, remove_result)
    workspace_docker._prune_superseded_images('bro/framework:cur')
    removals = [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']]
    assert removals == [
      ['docker', 'image', 'rm', 'bro/framework:in-use'],
      ['docker', 'image', 'rm', 'bro/framework:old'],
    ]

  def test_listing_failure_skips_pruning(self, monkeypatch):
    calls = self._patch_run(monkeypatch, _FakeProc(returncode=1, stderr='daemon down'))
    workspace_docker._prune_superseded_images('bro/framework:cur')
    assert [argv for argv in calls if argv[:3] == ['docker', 'image', 'rm']] == []


class TestPrepareContainer:
  def test_runs_the_shared_prepare_sequence_from_the_launch(self, monkeypatch, tmp_path):
    project = tmp_path / 'project'
    workspace = Workspace.create('ws', project, WorkspaceKind.CONTAINER)
    events: list = []
    monkeypatch.setattr(workspace_docker, 'image_tag', lambda: events.append('tag') or 'image')
    monkeypatch.setattr(
      workspace_docker, '_ensure_image', lambda tag: events.append(('ensure', tag))
    )
    monkeypatch.setattr(
      workspace_docker.credentials,
      'build_scoped_store',
      lambda secrets, optional=(): events.append(('store', secrets, optional)) or {'x': b'y'},
    )
    monkeypatch.setattr(workspace_docker, '_bro_tarball', lambda store: b'TARBALL')
    monkeypatch.setattr(
      workspace_docker,
      '_docker_create_argv',
      lambda *args, **kwargs: events.append(('argv', args, kwargs)) or ['docker', 'create'],
    )
    monkeypatch.setattr(
      workspace_docker,
      '_create_container',
      lambda argv, tarball, name: events.append(('create', argv, tarball, name)) or 'cid',
    )
    launch = workspace_docker.Launch(
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
    assert workspace_docker.prepare_container(launch, project) == 'cid'
    assert workspace.tree.is_dir()
    assert events[0:3] == [
      'tag',
      ('ensure', 'image'),
      ('store', ('github',), ('openai',)),
    ]
    argv_event = events[3]
    assert argv_event[0] == 'argv'
    assert argv_event[1] == ('image', 'ws', project, workspace.tree, 'worktree-ws', ['claude'])
    assert argv_event[2] == {
      'docker_sock': False,
      'extra_env': {'MARKER': 'x'},
      'forward_env': False,
      'tty': False,
      'extra_mounts': ['/host:/container'],
    }
    assert events[4] == ('create', ['docker', 'create'], b'TARBALL', 'ws')

  def test_local_trails_root_is_mounted_into_the_container(self, monkeypatch, tmp_path):
    project = tmp_path / 'project'
    Workspace.create('ws', project, WorkspaceKind.CONTAINER)
    trails_root = tmp_path / 'trail-data'
    captured: dict = {}
    monkeypatch.setenv('BRO_TRAILS_DIR', str(trails_root))
    monkeypatch.setattr(workspace_docker, 'image_tag', lambda: 'image')
    monkeypatch.setattr(workspace_docker, '_ensure_image', lambda tag: None)
    monkeypatch.setattr(
      workspace_docker.credentials,
      'build_scoped_store',
      lambda secrets, optional=(): {'trails.cred': b'{"backend":"local"}'},
    )
    monkeypatch.setattr(workspace_docker, '_bro_tarball', lambda store: b'TARBALL')
    monkeypatch.setattr(
      workspace_docker,
      '_docker_create_argv',
      lambda *args, **kwargs: captured.update(kwargs) or ['docker', 'create'],
    )
    monkeypatch.setattr(workspace_docker, '_create_container', lambda *args: 'cid')
    launch = workspace_docker.Launch(
      name='ws',
      command=['bro', 'run'],
      env={},
      secrets=('trails',),
      docker_sock=False,
      tty=False,
      forward_env=False,
    )

    assert workspace_docker.prepare_container(launch, project) == 'cid'
    assert captured['extra_env']['BRO_TRAILS_DIR'] == '/home/cw/.bro-trails'
    assert captured['extra_mounts'] == [f'{trails_root.resolve()}:/home/cw/.bro-trails']
    assert trails_root.is_dir()

  def test_service_trails_credential_adds_no_storage_mount(self, tmp_path, monkeypatch):
    monkeypatch.setenv('BRO_TRAILS_DIR', str(tmp_path))
    assert (
      workspace_docker._local_trails_root(
        {'trails.cred': b'{"base_url":"https://trails.example","token":"token"}'}
      )
      is None
    )


class TestDockerCreateArgv:
  @pytest.fixture
  def build_argv(self, monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_docker.Path, 'home', lambda: tmp_path)

    def build(**kwargs):
      return workspace_docker._docker_create_argv(
        'tag', 'ws', tmp_path / 'proj', tmp_path / 'tree', 'worktree-ws', ['claude'], **kwargs
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
    monkeypatch.setenv('CW_BRO', 'dev')
    assert 'CW_BRO' not in build_argv()
    assert 'CW_BRO=bro' in build_argv(extra_env={'CW_BRO': 'bro'})

  def test_forward_env_false_switches_the_forward_loop_off(self, build_argv, monkeypatch):
    # a broker-spawned child's environment is its LaunchSpec snapshot (extra_env)
    # only; none of the ambient _DOCKER_FORWARD_ENV vars may reach it
    for var in workspace_docker._DOCKER_FORWARD_ENV:
      monkeypatch.setenv(var, 'ambient')
    argv = build_argv(forward_env=False, extra_env={'MARKER': 'x'})
    assert not any(var in argv for var in workspace_docker._DOCKER_FORWARD_ENV)
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
