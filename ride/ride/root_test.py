from pathlib import Path
from unittest.mock import MagicMock

import bro.summon
import ride.artifacts
import ride.root
import ride.spawn
import ride.workspace.docker as workspace_docker
import ride.workspace.spawn as workspace_spawn
from bro.workspace.paths import workspace_dir
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets


def _workspace(tmp_path: Path, isolation: Isolation = Isolation.BOXED) -> Workspace:
  return Workspace.ensure('ws', tmp_path / 'project', isolation)


def _scope() -> ScopedSecrets:
  return ScopedSecrets({'github'}, {'openai'}, {'github': 'reviewer'})


def _docker_launch() -> workspace_docker.Launch:
  return workspace_docker.Launch(
    name='ws',
    command=['do-ride'],
    env={'RIDE_BRO': 'bro-dev'},
    secrets=('github',),
    optional_secrets=('openai',),
    credential_selection={'github': 'reviewer'},
    tty=True,
    forward_env=True,
    image='runtime-image',
    runtime_bundle_hash='bundle-hash',
  )


class TestDirectStartedParty:
  def test_boxed_prepare_then_attach_and_record(self, monkeypatch, tmp_path):
    monkeypatch.setattr(ride.root, 'broker_enabled', lambda: False)
    events: list[str] = []
    monkeypatch.setattr(
      ride.root,
      'prepare_container',
      lambda launch: events.append('prepare') or 'cid123',
    )
    monkeypatch.setattr(
      ride.root,
      'attach_interactive',
      lambda container_id: events.append(f'attach:{container_id}') or 7,
    )
    workspace = _workspace(tmp_path)

    code = ride.root.run_started_party(
      _docker_launch(),
      workspace,
      credential_scope=_scope(),
      container_runtime=MagicMock(),
      runtime_bundle=MagicMock(),
    )

    assert code == 7
    assert events == ['prepare', 'attach:cid123']
    assert (workspace_dir('ws') / 'exit').read_text() == '7'

  def test_process_run_clears_ambient_broker_facts(self, monkeypatch, tmp_path):
    monkeypatch.setattr(ride.root, 'broker_enabled', lambda: False)
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(ride.root.subprocess, 'run', run)
    workspace = _workspace(tmp_path, Isolation.UNBOXED)
    launch = ride.root.ProcessLaunch(
      command=['do-ride'],
      cwd=str(workspace.tree),
      env={
        'RIDE_BRO': 'bro-dev',
        'BROKER_CHANNEL': 'ambient-channel',
        'BROKER_UPSTREAM': 'ambient-upstream',
      },
      interactive=False,
    )

    assert (
      ride.root.run_started_party(
        launch,
        workspace,
        permits={'party.start.unboxed'},
        credential_scope=_scope(),
        container_runtime=MagicMock(),
        runtime_bundle=MagicMock(),
      )
      == 0
    )
    assert run.call_args.kwargs['env']['RIDE_PERMITS'] == 'party.start.unboxed'
    assert 'BROKER_CHANNEL' not in run.call_args.kwargs['env']
    assert 'BROKER_UPSTREAM' not in run.call_args.kwargs['env']


class TestManualStartedParty:
  def test_claims_a_boxed_party_after_prepare_and_before_attach(self, monkeypatch, tmp_path):
    events: list[str] = []
    monkeypatch.setattr(
      ride.root,
      'prepare_container',
      lambda launch: events.append('prepare') or 'cid123',
    )
    monkeypatch.setattr(
      ride.root,
      'attach_interactive',
      lambda container_id: events.append(f'attach:{container_id}') or 0,
    )

    assert (
      ride.root.run_manual_started_party(
        _docker_launch(),
        _workspace(tmp_path),
        credential_scope=_scope(),
        claim=lambda: events.append('claim'),
      )
      == 0
    )
    assert events == ['prepare', 'claim', 'attach:cid123']


class TestBrokerStartedParty:
  def test_wraps_a_boxed_root_with_its_authority_and_artifact_view(self, monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run_root(launch, **kwargs):
      captured['launch'] = launch
      captured.update(kwargs)
      return 3

    monkeypatch.setattr(ride.spawn, 'run_root_via_broker', fake_run_root)
    workspace = _workspace(tmp_path)

    assert (
      ride.root._run_via_broker(
        _docker_launch(),
        workspace,
        may_summon={'dev'},
        permits={'party.start.boxed'},
        summon_depth=4,
        summon_harness='claude',
        credential_scope=_scope(),
        container_runtime=MagicMock(),
        runtime_bundle=MagicMock(),
      )
      == 3
    )
    wrapped = captured['launch']
    assert isinstance(wrapped, workspace_spawn.DockerLaunchSpec)
    assert wrapped.launch.env[bro.summon.MAY_SUMMON_ENV] == 'dev'
    assert wrapped.launch.env[bro.summon.PERMITS_ENV] == 'party.start.boxed'
    assert wrapped.launch.extra_mounts == (ride.artifacts.view_mount('ws', 'ws'),)
    assert captured['workspace'] is workspace
    assert captured['credential_scope'] == _scope()

  def test_wraps_an_unboxed_root_as_a_process_spec(self, monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr(
      ride.spawn,
      'run_root_via_broker',
      lambda launch, **kwargs: captured.update(launch=launch, **kwargs) or 0,
    )
    workspace = _workspace(tmp_path, Isolation.UNBOXED)
    launch = ride.root.ProcessLaunch(
      command=['do-ride'],
      cwd=str(workspace.tree),
      env={'RIDE_BRO': 'bro-dev'},
      interactive=True,
    )

    assert (
      ride.root._run_via_broker(
        launch,
        workspace,
        may_summon=(),
        permits={'party.start.boxed'},
        summon_depth=2,
        summon_harness='bro',
        credential_scope=_scope(),
        container_runtime=MagicMock(),
        runtime_bundle=MagicMock(),
      )
      == 0
    )
    assert isinstance(captured['launch'], workspace_spawn.ProcessLaunchSpec)
    assert captured['launch'].interactive
