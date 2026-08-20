import sys

import bro.summon
import ride.root
import ride.spawn
import ride.summon_control
import ride.workspace.docker as workspace_docker
import ride.workspace.spawn as workspace_spawn
from bro.workspace.paths import CONTAINER_SUMMON_ROOT, summon_dir, workspace_dir
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace


def _exit_record(tmp_path) -> str:
  return (workspace_dir('ws') / 'exit').read_text()


def _workspace(tmp_path) -> Workspace:
  return Workspace.ensure('ws', tmp_path / 'project', WorkspaceKind.CONTAINER)


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestRunInContainerInjection:
  def test_prepare_then_start_sequence(self, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_DISABLED', '1')
    prepared: list = []
    monkeypatch.setattr(
      ride.root,
      'prepare_container',
      lambda launch: prepared.append(launch) or 'cid123',
    )
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
      calls.append(argv)
      return _FakeProc(returncode=7)

    monkeypatch.setattr(ride.root.subprocess, 'run', fake_run)
    launch = workspace_docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=(),
      docker_sock=True,
      tty=True,
      forward_env=True,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    assert ride.root.run_in_container(launch, _workspace(tmp_path)) == 7
    assert prepared == [launch]
    assert calls == [['docker', 'start', '-a', '-i', '--detach-keys=ctrl-z', 'cid123']]
    # the run's end is recorded on the workspace for `ride clean`
    assert _exit_record(tmp_path) == '7'

  def test_non_tty_launch_attaches_without_detach_keys(self, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_DISABLED', '1')
    monkeypatch.setattr(ride.root, 'prepare_container', lambda launch: 'cid123')
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
      calls.append(argv)
      return _FakeProc(returncode=0)

    monkeypatch.setattr(ride.root.subprocess, 'run', fake_run)
    launch = workspace_docker.Launch(
      name='ws',
      command=['bro', 'run'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    assert ride.root.run_in_container(launch, _workspace(tmp_path)) == 0
    # no pty, so no Ctrl+Z to intercept — and a zero exit must not probe the container
    assert calls == [['docker', 'start', '-a', 'cid123']]


class TestRunInContainerBrokerRoute:
  def test_run_in_container_routes_through_broker(self, monkeypatch, tmp_path):
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    roots: list = []

    def fake_root(launch, workspace, **kwargs):
      roots.append({'launch': launch, 'workspace': workspace, **kwargs})
      return 5

    monkeypatch.setattr(ride.root, '_run_root_via_broker', fake_root)
    launch = workspace_docker.Launch(
      name='ws',
      command=['claude'],
      env={},
      secrets=(),
      docker_sock=False,
      tty=True,
      forward_env=True,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    code = ride.root.run_in_container(launch, _workspace(tmp_path), may_summon={'dev'})
    assert code == 5
    [root] = roots
    assert root['launch'] is launch
    assert root['workspace'].name == 'ws'
    assert root['workspace'].kind is WorkspaceKind.CONTAINER
    assert root['may_summon'] == {'dev'}


class TestRunRootViaBroker:
  def test_builds_the_attached_launch_and_delegates(self, monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run_root(launch, *, workspace, may_summon, credential_scope, container_runtime):
      captured['launch'] = launch
      captured['workspace'] = workspace
      captured['may_summon'] = may_summon
      captured['credential_scope'] = credential_scope
      return 3

    monkeypatch.setattr(ride.spawn, 'run_root_via_broker', fake_run_root)
    launch = workspace_docker.Launch(
      name='ws',
      command=['claude', '--verbose'],
      env={'RIDE_BASE_REF': 'deadbeef'},
      secrets=('github',),
      optional_secrets=('openai',),
      docker_sock=True,
      tty=True,
      forward_env=True,
      image='runtime-image',
      runtime_bundle_hash='bundle-hash',
    )
    workspace = Workspace.create('ws', tmp_path / 'project', WorkspaceKind.CONTAINER)
    code = ride.root._run_root_via_broker(launch, workspace, may_summon={'dev'})
    assert code == 3
    assert captured['workspace'] is workspace
    assert captured['may_summon'] == {'dev'}
    assert captured['launch'] == workspace_spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='ws',
        command=['claude', '--verbose'],
        env={
          'RIDE_BASE_REF': 'deadbeef',
          ride.summon_control.STATUS_ENV: str(CONTAINER_SUMMON_ROOT / 'ws.status.json'),
          bro.summon.MAY_SUMMON_ENV: 'dev',
        },
        secrets=('github',),
        optional_secrets=('openai',),
        docker_sock=True,
        tty=True,
        forward_env=True,
        image='runtime-image',
        runtime_bundle_hash='bundle-hash',
        extra_mounts=(f'{summon_dir()}:{CONTAINER_SUMMON_ROOT}:ro',),
      ),
      capture_output=False,
    )
