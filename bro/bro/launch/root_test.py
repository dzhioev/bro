import sys

import bro.launch.root
import bro.launch.spawn
import bro.launch.summon_control
import workspace.docker
import workspace.spawn


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestRunInContainerInjection:
  def test_prepare_then_start_sequence(self, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(bro.launch.root, 'project_root', lambda: tmp_path / 'project')
    prepared: list = []
    monkeypatch.setattr(
      bro.launch.root,
      'prepare_container',
      lambda launch, project: prepared.append((launch, project)) or 'cid123',
    )
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
      calls.append(argv)
      return _FakeProc(returncode=7)

    monkeypatch.setattr(bro.launch.root.subprocess, 'run', fake_run)
    launch = workspace.docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=(),
      docker_sock=True,
      tty=True,
      forward_env=True,
    )
    assert bro.launch.root.run_in_container(launch) == 7
    assert prepared == [(launch, tmp_path / 'project')]
    assert calls == [['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']]
    # the run's end is recorded under the container-prefixed ref for `cw clean`
    assert (tmp_path / 'project' / 'var' / 'cw' / 'exit' / 'c:ws').read_text() == '7'

  def test_non_tty_launch_attaches_without_detach_keys(self, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(bro.launch.root, 'project_root', lambda: tmp_path / 'project')
    monkeypatch.setattr(bro.launch.root, 'prepare_container', lambda launch, project: 'cid123')
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
      calls.append(argv)
      return _FakeProc(returncode=0)

    monkeypatch.setattr(bro.launch.root.subprocess, 'run', fake_run)
    launch = workspace.docker.Launch(
      name='ws',
      command=['bro', 'run'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
    )
    assert bro.launch.root.run_in_container(launch) == 0
    # no pty, so no Ctrl+Z to intercept — and a zero exit must not probe the container
    assert calls == [['docker', 'start', '-a', 'cid123']]


class TestRunInContainerDrop:
  def _run(self, monkeypatch, tmp_path, *, exit_code: int) -> list:
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(bro.launch.root, 'project_root', lambda: tmp_path / 'project')
    monkeypatch.setattr(bro.launch.root, 'prepare_container', lambda launch, project: 'cid123')
    monkeypatch.setattr(
      bro.launch.root.subprocess, 'run', lambda *_a, **_k: _FakeProc(returncode=exit_code)
    )
    removed: list = []

    class _FakeWorkspace:
      def __init__(self, name, project):
        self.name = name

      def remove(self):
        removed.append(self.name)

    monkeypatch.setattr(bro.launch.root, 'ContainerWorkspace', _FakeWorkspace)
    launch = workspace.docker.Launch(
      name='ws',
      command=['bro', 'run'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
    )
    assert bro.launch.root.run_in_container(launch, drop=True) == exit_code
    return removed

  def test_drop_removes_the_workspace_after_a_clean_exit(self, monkeypatch, tmp_path):
    assert self._run(monkeypatch, tmp_path, exit_code=0) == ['ws']

  def test_drop_keeps_the_workspace_of_a_failed_run(self, monkeypatch, tmp_path):
    assert self._run(monkeypatch, tmp_path, exit_code=7) == []
    assert (tmp_path / 'project' / 'var' / 'cw' / 'exit' / 'c:ws').read_text() == '7'


class TestRunInContainerBrokerRoute:
  def test_run_in_container_routes_through_broker(self, monkeypatch, tmp_path):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(bro.launch.root, 'project_root', lambda: tmp_path / 'project')
    roots: list = []

    def fake_root(launch, project, **kwargs):
      roots.append({'launch': launch, 'project': project, **kwargs})
      return 5

    monkeypatch.setattr(bro.launch.root, '_run_root_via_broker', fake_root)
    launch = workspace.docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=True,
      forward_env=True,
    )
    code = bro.launch.root.run_in_container(launch, may_summon={'devoops'})
    assert code == 5
    assert roots == [
      {
        'launch': launch,
        'project': tmp_path / 'project',
        'may_summon': {'devoops'},
        'trail_pointer': None,
      }
    ]


class TestRunRootViaBroker:
  def test_builds_the_attached_launch_and_delegates(self, monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run_root(launch, project, *, session, may_summon, credential_scope, trail_pointer):
      captured['launch'] = launch
      captured['project'] = project
      captured['session'] = session
      captured['may_summon'] = may_summon
      captured['credential_scope'] = credential_scope
      captured['trail_pointer'] = trail_pointer
      return 3

    monkeypatch.setattr(bro.launch.spawn, 'run_root_via_broker', fake_run_root)
    launch = workspace.docker.Launch(
      name='ws',
      command=['claude', '--verbose'],
      env={'CW_BASE_REF': 'deadbeef'},
      secrets=('github',),
      optional_secrets=('openai',),
      docker_sock=True,
      tty=True,
      forward_env=True,
    )
    code = bro.launch.root._run_root_via_broker(
      launch, tmp_path / 'project', may_summon={'devoops'}, trail_pointer=None
    )
    assert code == 3
    assert captured['project'] == tmp_path / 'project'
    # the session key carries the container-mode prefix, so a same-name host
    # session keeps its own summon state files
    assert captured['session'] == 'c:ws'
    assert captured['may_summon'] == {'devoops'}
    assert captured['launch'] == workspace.spawn.DockerLaunchSpec(
      workspace.docker.Launch(
        name='ws',
        command=['claude', '--verbose'],
        env={
          'CW_BASE_REF': 'deadbeef',
          bro.launch.summon_control.STATUS_ENV: '/host-repo/var/cw/summon/c:ws.status.json',
        },
        secrets=('github',),
        optional_secrets=('openai',),
        docker_sock=True,
        tty=True,
        forward_env=True,
      )
    )
