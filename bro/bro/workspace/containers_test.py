import sys

import cw.containers
import cw.docker
import cw.spawn
import cw.summon


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestRunInContainerInjection:
  def test_prepare_then_start_sequence(self, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(cw.containers, '_project_root', lambda: tmp_path / 'project')
    prepared: list = []
    monkeypatch.setattr(
      cw.containers,
      'prepare_container',
      lambda launch, project: prepared.append((launch, project)) or 'cid123',
    )
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
      calls.append(argv)
      return _FakeProc(returncode=7)

    monkeypatch.setattr(cw.containers.subprocess, 'run', fake_run)
    launch = cw.docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=(),
      docker_sock=True,
      tty=True,
      forward_env=True,
    )
    assert cw.containers.run_in_container(launch) == 7
    assert prepared == [(launch, tmp_path / 'project')]
    assert calls == [['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']]

  def test_non_tty_launch_attaches_without_detach_keys(self, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(cw.containers, '_project_root', lambda: tmp_path / 'project')
    monkeypatch.setattr(cw.containers, 'prepare_container', lambda launch, project: 'cid123')
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
      calls.append(argv)
      return _FakeProc(returncode=0)

    monkeypatch.setattr(cw.containers.subprocess, 'run', fake_run)
    launch = cw.docker.Launch(
      name='ws',
      command=['bro', 'run'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
    )
    assert cw.containers.run_in_container(launch) == 0
    # no pty, so no Ctrl+Z to intercept — and a zero exit must not probe the container
    assert calls == [['docker', 'start', '-a', 'cid123']]


class TestAttachInteractive:
  def _harness(self, monkeypatch, codes: list[int], running: list[bool]):
    events: list = []
    code_iterator = iter(codes)
    running_iterator = iter(running)

    def fake_run(argv, *args, **kwargs):
      events.append(argv)
      return _FakeProc(returncode=next(code_iterator))

    monkeypatch.setattr(cw.containers.subprocess, 'run', fake_run)
    monkeypatch.setattr(cw.containers, 'container_running', lambda cid: next(running_iterator))
    monkeypatch.setattr(
      cw.containers, 'suspend_until_continued', lambda cid: events.append(('suspend', cid))
    )
    return events

  def test_detach_suspends_and_reattaches(self, monkeypatch):
    events = self._harness(monkeypatch, codes=[0, 4], running=[True])
    assert cw.containers._attach_interactive('cid123') == 4
    assert events == [
      ['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123'],
      ('suspend', 'cid123'),
      ['docker', 'attach', '--detach-keys=ctrl-z', 'cid123'],
    ]

  def test_container_exit_returns_without_suspend(self, monkeypatch):
    events = self._harness(monkeypatch, codes=[0], running=[False])
    assert cw.containers._attach_interactive('cid123') == 0
    assert events == [['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']]

  def test_client_death_returns_without_probing_the_container(self, monkeypatch):
    # a nonzero client exit is never a detach (the detach key exits 0); the running
    # probe must not even run — the daemon may be the very thing that just failed
    events = self._harness(monkeypatch, codes=[130], running=[])
    assert cw.containers._attach_interactive('cid123') == 130
    assert events == [['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']]


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

    def fake_root(launch, project, **kwargs):
      roots.append({'launch': launch, 'project': project, **kwargs})
      return 5

    monkeypatch.setattr(cw.containers, '_run_root_via_broker', fake_root)
    launch = cw.docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=True,
      forward_env=True,
    )
    code = cw.containers.run_in_container(launch, may_summon={'devoops'})
    assert code == 5
    assert roots == [
      {
        'launch': launch,
        'project': tmp_path / 'project',
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
    launch = cw.docker.Launch(
      name='ws',
      command=['claude', '--verbose'],
      env={'CW_BASE_REF': 'deadbeef'},
      secrets=('github',),
      optional_secrets=('openai',),
      docker_sock=True,
      tty=True,
      forward_env=True,
    )
    code = cw.containers._run_root_via_broker(launch, tmp_path / 'project', may_summon={'devoops'})
    assert code == 3
    assert captured['project'] == tmp_path / 'project'
    # the session key carries the container-mode prefix, so a same-name host
    # session keeps its own summon state files
    assert captured['session'] == 'c:ws'
    assert captured['may_summon'] == {'devoops'}
    assert captured['launch'] == cw.spawn.DockerLaunchSpec(
      cw.docker.Launch(
        name='ws',
        command=['claude', '--verbose'],
        env={
          'CW_BASE_REF': 'deadbeef',
          cw.summon.STATUS_ENV: '/host-repo/var/cw/summon/c:ws.status.json',
        },
        secrets=('github',),
        optional_secrets=('openai',),
        docker_sock=True,
        tty=True,
        forward_env=True,
      )
    )
