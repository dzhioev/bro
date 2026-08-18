import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import bro.launch.bro_run
import bro.launch.identity
import bro.launch.scope
import bro.launch.spawn
import bro.launch.summon_control
import bro.workspace.docker as workspace_docker
import bro.workspace.store as workspace_store
from bro.monitor import trail_pointer
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from bro.workspace.paths import broker_dir, summon_dir

PARENT_WORKSPACE = Path('/var/ride/0123456789abcdef/workspaces/parent/tree')
SUMMONER = {'session': 'ws'}


class TestSummonLowering:
  @pytest.fixture
  def lowering_harness(self, monkeypatch, tmp_path):
    monkeypatch.setattr(bro.launch.spawn, 'project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(
      bro.launch.scope,
      'scoped_secrets',
      lambda name, surface, llm_spec=None: workspace_store.ScopedSecrets(
        required={'aws', 'trails'}, optional={'openai'}, docker_sock=True
      ),
    )
    monkeypatch.setattr(bro.launch.bro_run, 'local_trails_mounts', lambda scoped: ())
    monkeypatch.setattr(
      bro.launch.spawn,
      'resolve_head',
      lambda root, repository: 'PARENT-SHA' if repository == PARENT_WORKSPACE else None,
    )
    monkeypatch.setattr(
      bro.launch.spawn, 'resolve_ref', lambda root, ref: 'REF-SHA' if ref == 'summon' else None
    )

  def test_lowers_to_the_bro_run_docker_launch(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
    )
    lowered = bro.launch.spawn._lower_summon(launch, 'broker-CH')
    assert lowered == bro.launch.spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='broker-CH',
        command=['bro', 'run', 'dev', 'deploy the thing', '--in-place'],
        env={
          'RIDE_BASE_REF': 'PARENT-SHA',
          'RIDE_BRO': 'dev',
          'RIDE_MAY_SUMMON': '',
          'RIDE_SUMMONER': '{"session":"ws"}',
          **bro.launch.identity.bro_git_identity_env('dev'),
        },
        secrets={'aws', 'trails'},
        optional_secrets={'openai'},
        docker_sock=True,
        tty=False,
        forward_env=False,
      ),
    )

  def test_lowering_logs_the_scope_like_any_container_launch(self, lowering_harness, caplog):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev', prompt='p', parent_workspace=PARENT_WORKSPACE, summoner=SUMMONER, may_summon=()
    )
    with caplog.at_level('INFO'):
      bro.launch.spawn._lower_summon(launch, 'broker-CH')
    assert 'scoped secrets for summoned dev: aws, trails' in caplog.text

  def test_hold_rides_the_childs_inner_argv(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      hold='attended',
    )
    lowered = bro.launch.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.command == [
      'bro', 'run', 'dev', 'deploy the thing', '--hold', 'attended', '--in-place',
    ]  # fmt: skip

  def test_the_llm_recipe_rides_the_childs_inner_argv(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      llm='openai:sol:high+fast',
    )
    lowered = bro.launch.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.command == [
      'bro', 'run', 'dev', 'deploy the thing', '--llm', 'openai:sol:high+fast', '--in-place',
    ]  # fmt: skip

  def test_credential_overrides_adjust_the_childs_scope(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      grant_credentials=('gmail_creds',),
      revoke_credentials=('openai',),
    )
    lowered = bro.launch.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.secrets == {'aws', 'trails', 'gmail_creds'}
    assert lowered.launch.optional_secrets == set()

  def test_no_op_credential_override_fails_the_spawn(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      grant_credentials=('aws',),
    )
    with pytest.raises(ValueError, match='already in the scoped credential set'):
      bro.launch.spawn._lower_summon(launch, 'broker-CH')

  def test_the_childs_own_allow_list_rides_its_environment(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=('bro', 'reviewer'),
    )
    lowered = bro.launch.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.env['RIDE_MAY_SUMMON'] == 'bro,reviewer'

  def test_into_overrides_the_inherited_base_ref(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      into='summon',
    )
    assert bro.launch.spawn._lower_summon(launch, 'broker-CH').launch.env == {
      'RIDE_BASE_REF': 'REF-SHA',
      'RIDE_BRO': 'dev',
      'RIDE_MAY_SUMMON': '',
      'RIDE_SUMMONER': '{"session":"ws"}',
      **bro.launch.identity.bro_git_identity_env('dev'),
    }

  def test_unresolvable_into_fails_the_spawn(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      into='nope',
    )
    with pytest.raises(ValueError, match='nope'):
      bro.launch.spawn._lower_summon(launch, 'broker-CH')

  def test_unreadable_parent_head_fails_the_spawn(self, lowering_harness):
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev', prompt='p', parent_workspace=Path('/gone'), summoner=SUMMONER, may_summon=()
    )
    with pytest.raises(ValueError, match="summoner's HEAD"):
      bro.launch.spawn._lower_summon(launch, 'broker-CH')

  @pytest.mark.asyncio
  async def test_spawner_lowers_off_loop_and_delegates_to_docker(self, lowering_harness):
    class RecordingDocker(bro.launch.spawn.DockerSpawner):
      def __init__(self):
        self.spawned: list = []

      async def spawn(self, launch, channel):
        self.spawned.append((launch, channel))
        return MagicMock()

    docker = RecordingDocker()
    spawner = bro.launch.spawn.SummonSpawner(docker)
    channel = bro.launch.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev', prompt='p', parent_workspace=PARENT_WORKSPACE, summoner=SUMMONER, may_summon=()
    )
    await spawner.spawn(launch, channel)
    [(lowered, lowered_channel)] = docker.spawned
    assert isinstance(lowered, bro.launch.spawn.DockerLaunchSpec)
    assert lowered.launch.command == ['bro', 'run', 'dev', 'p', '--in-place']
    assert lowered.launch.name == 'broker-CH'
    assert lowered_channel is channel

  @pytest.mark.asyncio
  async def test_lowering_failure_propagates_out_of_spawn(self, lowering_harness):
    spawner = bro.launch.spawn.SummonSpawner(bro.launch.spawn.DockerSpawner())
    channel = bro.launch.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = bro.launch.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      into='nope',
    )
    # the raise crosses to_thread back onto the loop: Dispatcher.spawn turns it
    # into the correlated failed{reason: 'launch'}
    with pytest.raises(ValueError, match='nope'):
      await spawner.spawn(launch, channel)


class TestRunRootViaBroker:
  def test_wires_control_dir_composite_spawner_handlers_and_run(self, monkeypatch, tmp_path):
    captured: dict = {}

    class FakeBroker:
      def __init__(self, transport, spawner, **kwargs):
        captured['transport'] = transport
        captured['spawner'] = spawner
        captured['handlers'] = {}
        captured['observers'] = []

      def on(self, message_type, handler):
        captured['handlers'][message_type] = handler

      def add_delivery_observer(self, observer):
        captured['observers'].append(observer)

      def run(self, launch):
        captured['launch'] = launch
        return 3

    monkeypatch.setattr(bro.launch.spawn, 'Broker', FakeBroker)
    launch = bro.launch.spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    workspace = Workspace.create('ws', tmp_path / 'proj', WorkspaceKind.CONTAINER)
    assert bro.launch.spawn.run_root_via_broker(launch, workspace=workspace) == 3
    assert captured['transport']._dir == broker_dir(tmp_path / 'proj')
    # the composite over both launch modes plus the summon lowering: any root can
    # spawn docker children, summons included
    spawner = captured['spawner']
    assert isinstance(spawner, bro.launch.spawn.CompositeSpawner)
    docker_spawner = spawner._spawners[bro.launch.spawn.DockerLaunchSpec]
    process_spawner = spawner._spawners[bro.launch.spawn.ProcessLaunchSpec]
    assert isinstance(docker_spawner, bro.launch.spawn.DockerSpawner)
    assert isinstance(process_spawner, bro.launch.spawn.ProcessSpawner)
    assert isinstance(
      spawner._spawners[bro.launch.spawn.SummonLaunchSpec], bro.launch.spawn.SummonSpawner
    )
    # both attached-capable spawners point at the same per-session host log
    host_log = workspace.host_log
    assert docker_spawner._host_log == host_log
    assert process_spawner._host_log == host_log
    assert set(captured['handlers']) == {'ping', 'started', 'completed', 'summon'}
    assert captured['handlers']['ping'] is bro.launch.spawn.ping_handler
    assert captured['handlers']['completed'] is bro.launch.spawn._log_root_completed
    # the summon handler and the delivery tap belong to the same per-root control
    control = captured['handlers']['summon'].__self__
    assert isinstance(control, bro.launch.summon_control.SummonControl)
    assert [observer.__self__ for observer in captured['observers']] == [control]
    assert control._workspace is workspace
    state_directory = summon_dir(tmp_path / 'proj')
    assert control._status_file == state_directory / 'ws.status.json'
    assert control._audit_file == state_directory / 'ws.jsonl'
    assert captured['launch'] is launch

  def test_root_lifecycle_handlers_log_trail_and_end_reason(self, caplog, tmp_path):
    from bro.broker.brotocol import Message, Tag
    from bro.broker.dispatcher import Dispatcher

    dispatcher = Dispatcher()
    workspace = Workspace.create('ws', tmp_path, WorkspaceKind.CONTAINER)
    control = bro.launch.summon_control.SummonControl(
      allow_list=set(),
      workspace=workspace,
      status_file=tmp_path / 'status.json',
      audit_file=tmp_path / 'audit.jsonl',
    )
    bro.launch.spawn._note_root_started(control, workspace)(
      dispatcher, 'root', Message(type=Tag.STARTED, payload={'trail_id': 't-1'})
    )
    # the started handler doubles as the bro-run root's provenance source
    assert control._root_trail_id == 't-1'
    pointer = trail_pointer.broker_pointer(workspace.path)
    assert json.loads(pointer.read_text()) == {'trail_id': 't-1'}
    bro.launch.spawn._log_root_completed(
      dispatcher,
      'root',
      Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'ok'}),
    )
    # a raised run surfaces its reason — the result is the failure cause
    bro.launch.spawn._log_root_completed(
      dispatcher,
      'root',
      Message(type=Tag.COMPLETED, payload={'result': 'no api key', 'end_reason': 'raised'}),
    )
    assert any('root run started (trail t-1)' in record.message for record in caplog.records)
    assert any('root run ended: ok' in record.message for record in caplog.records)
    assert any('root run raised: no api key' in record.message for record in caplog.records)
