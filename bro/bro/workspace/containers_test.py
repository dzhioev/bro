import sys

import pytest

import cw.containers
import cw.spawn
import cw.summon


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestRunInContainerInjection:
  @pytest.fixture
  def harness(self, monkeypatch, tmp_path):
    # pin the broker-less direct path; the broker gate and root launch have their own tests
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(cw.containers, '_project_root', lambda: tmp_path / 'project')
    monkeypatch.setattr(cw.containers, '_image_tag', lambda: 'tag')
    monkeypatch.setattr(cw.containers, '_ensure_image', lambda tag: None)
    monkeypatch.setattr(cw.containers.Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(
      cw.containers, '_docker_create_argv', lambda *a, **k: ['docker', 'create', 'ARGS']
    )
    monkeypatch.setattr(
      cw.containers.credentials,
      'build_scoped_store',
      lambda names, optional=(): {'credentials.json': b'{}'},
    )
    calls: list = []

    def fake_run(argv, *a, **k):
      calls.append({'argv': argv, 'input': k.get('input')})
      if argv[0] == 'git':
        return _FakeProc(returncode=0)
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:2] == ['docker', 'start']:
        return _FakeProc(returncode=7)
      return _FakeProc(returncode=0)  # cp, rm

    monkeypatch.setattr(cw.containers.subprocess, 'run', fake_run)
    return calls

  def test_create_cp_start_sequence(self, harness):
    import io
    import tarfile

    code = cw.containers.run_in_container('ws', ['claude'])
    assert code == 7  # propagates `docker start` exit code
    docker_calls = [c for c in harness if c['argv'][0] == 'docker']
    assert docker_calls[0]['argv'][:2] == ['docker', 'create']
    # the scoped store is cp'd into the pre-start container as a tar on stdin
    cp = next(c for c in harness if c['argv'][:3] == ['docker', 'cp', '-'])
    assert cp['argv'][3] == 'cid123:/home/cw'
    assert isinstance(cp['input'], bytes)
    with tarfile.open(fileobj=io.BytesIO(cp['input']), mode='r') as tar:
      assert '.ppp/credentials.json' in tar.getnames()
    # last call is the run-equivalent `docker start -a -i <id>`
    assert harness[-1]['argv'] == ['docker', 'start', '-a', '-i', 'cid123']


class TestBrokerGate:
  def test_disabled_by_env(self, monkeypatch):
    # presence-checked: any value disables, and the check precedes any broker import
    monkeypatch.setenv('BROKER_DISABLED', '')
    assert cw.containers._broker_enabled() is False

  def test_unimportable_broker_degrades(self, monkeypatch):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setitem(sys.modules, 'broker', None)  # import machinery raises ImportError
    assert cw.containers._broker_enabled() is False

  def test_enabled_by_default(self, monkeypatch):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    assert cw.containers._broker_enabled() is True

  def test_container_gate_degrades_on_macos(self, monkeypatch):
    # the daemon runs in a VM there — the channel socket can't be bind-mounted
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert cw.containers._container_broker_enabled() is False

  def test_container_gate_delegates_off_macos(self, monkeypatch):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert cw.containers._container_broker_enabled() is True

  def test_run_in_container_routes_through_broker(self, monkeypatch, tmp_path):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(cw.containers, '_project_root', lambda: tmp_path / 'project')
    roots: list = []

    def fake_root(name, command, project, **kwargs):
      roots.append({'name': name, 'command': command, 'project': project, **kwargs})
      return 5

    monkeypatch.setattr(cw.containers, '_run_root_via_broker', fake_root)
    code = cw.containers.run_in_container(
      'ws', ['claude'], docker_sock=False, may_summon={'devoops'}
    )
    assert code == 5
    assert roots == [
      {
        'name': 'ws',
        'command': ['claude'],
        'project': tmp_path / 'project',
        'secrets': (),
        'optional_secrets': (),
        'docker_sock': False,
        'extra_env': None,
        'forward_bro': True,
        'may_summon': {'devoops'},
      }
    ]


class TestRunRootViaBroker:
  def test_builds_the_attached_launch_and_delegates(self, monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run_root(launch, project, *, session, may_summon):
      captured['launch'] = launch
      captured['project'] = project
      captured['session'] = session
      captured['may_summon'] = may_summon
      return 3

    monkeypatch.setattr(cw.spawn, 'run_root_via_broker', fake_run_root)
    code = cw.containers._run_root_via_broker(
      'ws',
      ['claude', '--auto'],
      tmp_path / 'project',
      secrets=('github',),
      optional_secrets=('openai',),
      docker_sock=True,
      extra_env={'CW_BASE_REF': 'deadbeef'},
      forward_bro=True,
      may_summon={'devoops'},
    )
    assert code == 3
    assert captured['project'] == tmp_path / 'project'
    # the session key carries the container-mode prefix, so a same-name host
    # session keeps its own summon state files
    assert captured['session'] == 'c:ws'
    assert captured['may_summon'] == {'devoops'}
    assert captured['launch'] == cw.spawn.DockerLaunchSpec(
      command=['claude', '--auto'],
      # the summon-status env rides in next to the caller's env: the container
      # reads the file the host writes through its read-only /host-repo mount
      env={
        'CW_BASE_REF': 'deadbeef',
        cw.summon.STATUS_ENV: '/host-repo/var/cw/summon/c:ws.status.json',
      },
      secrets=('github',),
      attached=True,
      name='ws',
      optional_secrets=('openai',),
      docker_sock=True,
      forward_bro=True,
    )
