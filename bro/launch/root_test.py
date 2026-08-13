import sys

import bro.launch.root
import bro.launch.spawn
import bro.launch.summon_control
import bro.summon
import bro.workspace.docker as workspace_docker
import bro.workspace.spawn as workspace_spawn
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace


def _exit_record(tmp_path) -> str:
  return (tmp_path / 'project' / 'var' / 'cw' / 'workspaces' / 'ws' / 'exit').read_text()


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
    launch = workspace_docker.Launch(
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
    # the run's end is recorded on the workspace for `cw clean`
    assert _exit_record(tmp_path) == '7'

  def test_non_tty_launch_attaches_without_detach_keys(self, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(bro.launch.root, 'project_root', lambda: tmp_path / 'project')
    monkeypatch.setattr(bro.launch.root, 'prepare_container', lambda launch, project: 'cid123')
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
      calls.append(argv)
      return _FakeProc(returncode=0)

    monkeypatch.setattr(bro.launch.root.subprocess, 'run', fake_run)
    launch = workspace_docker.Launch(
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
    monkeypatch.setattr(Workspace, 'remove', lambda workspace: removed.append(workspace.name))
    launch = workspace_docker.Launch(
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
    assert _exit_record(tmp_path) == '7'


class TestRunInContainerBrokerRoute:
  def test_run_in_container_routes_through_broker(self, monkeypatch, tmp_path):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(bro.launch.root, 'project_root', lambda: tmp_path / 'project')
    roots: list = []

    def fake_root(launch, workspace, **kwargs):
      roots.append({'launch': launch, 'workspace': workspace, **kwargs})
      return 5

    monkeypatch.setattr(bro.launch.root, '_run_root_via_broker', fake_root)
    launch = workspace_docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=True,
      forward_env=True,
    )
    code = bro.launch.root.run_in_container(launch, may_summon={'dev'})
    assert code == 5
    [root] = roots
    assert root['launch'] is launch
    assert root['workspace'].name == 'ws'
    assert root['workspace'].kind is WorkspaceKind.CONTAINER
    assert root['may_summon'] == {'dev'}
    assert root['trail_pointer'] is None


class TestRunRootViaBroker:
  def test_builds_the_attached_launch_and_delegates(self, monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run_root(launch, *, workspace, may_summon, credential_scope, trail_pointer):
      captured['launch'] = launch
      captured['workspace'] = workspace
      captured['may_summon'] = may_summon
      captured['credential_scope'] = credential_scope
      captured['trail_pointer'] = trail_pointer
      return 3

    monkeypatch.setattr(bro.launch.spawn, 'run_root_via_broker', fake_run_root)
    launch = workspace_docker.Launch(
      name='ws',
      command=['claude', '--verbose'],
      env={'CW_BASE_REF': 'deadbeef'},
      secrets=('github',),
      optional_secrets=('openai',),
      docker_sock=True,
      tty=True,
      forward_env=True,
    )
    workspace = Workspace.create('ws', tmp_path / 'project', WorkspaceKind.CONTAINER)
    code = bro.launch.root._run_root_via_broker(
      launch, workspace, may_summon={'dev'}, trail_pointer=None
    )
    assert code == 3
    assert captured['workspace'] is workspace
    assert captured['may_summon'] == {'dev'}
    assert captured['launch'] == workspace_spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='ws',
        command=['claude', '--verbose'],
        env={
          'CW_BASE_REF': 'deadbeef',
          bro.launch.summon_control.STATUS_ENV: '/host-repo/var/cw/summon/ws.status.json',
          bro.summon.MAY_SUMMON_ENV: 'dev',
        },
        secrets=('github',),
        optional_secrets=('openai',),
        docker_sock=True,
        tty=True,
        forward_env=True,
      )
    )
