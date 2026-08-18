import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import bro.workspace.docker as workspace_docker
import bro.workspace.store as workspace_store
import ride.bro
import ride.bro_run
import ride.identity
import ride.scope
import ride.session
import ride.spawn
import ride.summon_control
from bro.monitor import trail_pointer
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from bro.workspace.paths import broker_dir, summon_dir, workspace_dir

PARENT_WORKSPACE = Path('/var/ride/0123456789abcdef/workspaces/parent/tree')
SUMMONER = {'session': 'ws'}


class TestSummonLowering:
  @pytest.fixture
  def lowering_harness(self, monkeypatch, tmp_path):
    monkeypatch.setattr(ride.spawn, 'project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(
      ride.scope,
      'scoped_secrets',
      lambda name, surface, llm_spec=None: workspace_store.ScopedSecrets(
        required={'aws', 'trails'}, optional={'openai'}, docker_sock=True
      ),
    )
    monkeypatch.setattr(ride.bro_run, 'local_trails_mounts', lambda scoped: ())
    monkeypatch.setattr(
      ride.spawn,
      'resolve_head',
      lambda root, repository: 'PARENT-SHA' if repository == PARENT_WORKSPACE else None,
    )
    monkeypatch.setattr(
      ride.spawn, 'resolve_ref', lambda root, ref: 'REF-SHA' if ref == 'summon' else None
    )

  def test_lowers_to_the_bro_run_docker_launch(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH')
    assert lowered == ride.spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='broker-CH',
        command=['bro', 'run', 'dev', 'deploy the thing', '--hold', 'unattended', '--in-place'],
        env={
          'RIDE_BASE_REF': 'PARENT-SHA',
          'RIDE_BRO': 'dev',
          'RIDE_MAY_SUMMON': '',
          'RIDE_SUMMONER': '{"session":"ws"}',
          **ride.identity.bro_git_identity_env('dev'),
        },
        secrets={'aws', 'trails'},
        optional_secrets={'openai'},
        docker_sock=True,
        tty=False,
        forward_env=False,
      ),
    )

  def test_lowering_logs_the_scope_like_any_container_launch(self, lowering_harness, caplog):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev', prompt='p', parent_workspace=PARENT_WORKSPACE, summoner=SUMMONER, may_summon=()
    )
    with caplog.at_level('INFO'):
      ride.spawn._lower_summon(launch, 'broker-CH')
    assert 'scoped secrets for summoned dev: aws, trails' in caplog.text

  def test_hold_rides_the_childs_inner_argv(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      hold='attended',
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.command == [
      'bro', 'run', 'dev', 'deploy the thing', '--hold', 'attended', '--in-place',
    ]  # fmt: skip

  def test_the_llm_recipe_rides_the_childs_inner_argv(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      llm='openai:sol:high+fast',
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.command == [
      'bro', 'run', 'dev', 'deploy the thing',
      '--llm', 'openai:sol:high+fast', '--hold', 'unattended', '--in-place',
    ]  # fmt: skip

  def test_credential_overrides_adjust_the_childs_scope(self, lowering_harness):
    # only the credential halves reach the scope; the `@bro` half was already
    # resolved into may_summon by the control
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      grant=('gmail_creds', '@reviewer'),
      revoke=('openai',),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.secrets == {'aws', 'trails', 'gmail_creds'}
    assert lowered.launch.optional_secrets == set()

  def test_no_op_credential_override_fails_the_spawn(self, lowering_harness, tmp_path):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      grant=('aws',),
    )
    with pytest.raises(ValueError, match='already in the scoped credential set'):
      ride.spawn._lower_summon(launch, 'broker-CH')
    # every fallible resolution precedes the workspace record, so nothing to
    # reclaim is left behind
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH', tmp_path / 'proj')

  def test_lowering_records_the_childs_resume_spec(self, lowering_harness, tmp_path):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      hold='guided',
      llm='openai:sol:high',
      grant=('gmail_creds', '@reviewer'),
      revoke=('openai',),
    )
    ride.spawn._lower_summon(launch, 'broker-CH')
    workspace = Workspace.open('broker-CH', tmp_path / 'proj')
    assert workspace.metadata.throwaway
    assert (
      ride.session.load_resume_spec(workspace)
      == ride.session.SessionSpec(
        name='broker-CH',
        harness='bro',
        workspace_pinned=False,
        host=False,
        drop=True,
        no_trails=False,
        hold='guided',
        grant=['gmail_creds', '@reviewer'],
        revoke=['openai'],
        llm='openai:sol:high',
        resolved_llm=ride.bro.BRO.resolve_llm('openai:sol:high', 'dev').dump(),
        solo=True,
        resume=False,
        into=None,
        bro='dev',
        prompt='deploy the thing',
        subject='deploy the thing',
        arguments=[],
        harness_options={},
      ).resume_variant()
    )

  def test_the_childs_own_allow_list_rides_its_environment(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=('bro', 'reviewer'),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH')
    assert lowered.launch.env['RIDE_MAY_SUMMON'] == 'bro,reviewer'

  def test_into_overrides_the_inherited_base_ref(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      into='summon',
    )
    assert ride.spawn._lower_summon(launch, 'broker-CH').launch.env == {
      'RIDE_BASE_REF': 'REF-SHA',
      'RIDE_BRO': 'dev',
      'RIDE_MAY_SUMMON': '',
      'RIDE_SUMMONER': '{"session":"ws"}',
      **ride.identity.bro_git_identity_env('dev'),
    }

  def test_unresolvable_into_fails_the_spawn(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent_workspace=PARENT_WORKSPACE,
      summoner=SUMMONER,
      may_summon=(),
      into='nope',
    )
    with pytest.raises(ValueError, match='nope'):
      ride.spawn._lower_summon(launch, 'broker-CH')

  def test_unreadable_parent_head_fails_the_spawn(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev', prompt='p', parent_workspace=Path('/gone'), summoner=SUMMONER, may_summon=()
    )
    with pytest.raises(ValueError, match="summoner's HEAD"):
      ride.spawn._lower_summon(launch, 'broker-CH')

  @pytest.mark.asyncio
  async def test_spawner_lowers_off_loop_and_delegates_to_docker(self, lowering_harness):
    class RecordingDocker(ride.spawn.DockerSpawner):
      def __init__(self):
        self.spawned: list = []

      async def spawn(self, launch, channel):
        self.spawned.append((launch, channel))
        return MagicMock()

    docker = RecordingDocker()
    spawner = ride.spawn.SummonSpawner(docker)
    channel = ride.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = ride.spawn.SummonLaunchSpec(
      target='dev', prompt='p', parent_workspace=PARENT_WORKSPACE, summoner=SUMMONER, may_summon=()
    )
    await spawner.spawn(launch, channel)
    [(lowered, lowered_channel)] = docker.spawned
    assert isinstance(lowered, ride.spawn.DockerLaunchSpec)
    assert lowered.launch.command == [
      'bro',
      'run',
      'dev',
      'p',
      '--hold',
      'unattended',
      '--in-place',
    ]
    assert lowered.launch.name == 'broker-CH'
    assert lowered_channel is channel

  @pytest.mark.asyncio
  async def test_lowering_failure_propagates_out_of_spawn(self, lowering_harness):
    spawner = ride.spawn.SummonSpawner(ride.spawn.DockerSpawner())
    channel = ride.spawn.Provisioned(channel='CH', host_endpoint='/host/CH.sock')
    launch = ride.spawn.SummonLaunchSpec(
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


class TestChildTrailPublication:
  def test_started_delivery_publishes_the_childs_broker_pointer(self, tmp_path):
    from bro.broker.brotocol import Message, Tag

    observe = ride.spawn._note_child_started(tmp_path / 'proj')
    observe('CH', 'root', Message(type=Tag.STARTED, payload={'trail_id': 't-9'}, in_reply_to='req'))
    pointer = trail_pointer.broker_pointer(workspace_dir(tmp_path / 'proj', 'broker-CH'))
    assert trail_pointer.read(pointer) == 't-9'

  def test_non_started_and_trailless_deliveries_publish_nothing(self, tmp_path):
    from bro.broker.brotocol import Message, Tag

    project = tmp_path / 'proj'
    observe = ride.spawn._note_child_started(project)
    observe('CH', 'root', Message(type=Tag.COMPLETED, payload={'trail_id': 't'}, in_reply_to='r'))
    observe(None, 'root', Message(type=Tag.STARTED, payload={'trail_id': 't'}, in_reply_to='r'))
    observe('CH', 'root', Message(type=Tag.STARTED, payload={}, in_reply_to='r'))
    assert not trail_pointer.broker_pointer(workspace_dir(project, 'broker-CH')).exists()


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

    monkeypatch.setattr(ride.spawn, 'Broker', FakeBroker)
    launch = ride.spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    workspace = Workspace.create('ws', tmp_path / 'proj', WorkspaceKind.CONTAINER)
    assert ride.spawn.run_root_via_broker(launch, workspace=workspace) == 3
    assert captured['transport']._dir == broker_dir(tmp_path / 'proj')
    # the composite over both launch modes plus the summon lowering: any root can
    # spawn docker children, summons included
    spawner = captured['spawner']
    assert isinstance(spawner, ride.spawn.CompositeSpawner)
    docker_spawner = spawner._spawners[ride.spawn.DockerLaunchSpec]
    process_spawner = spawner._spawners[ride.spawn.ProcessLaunchSpec]
    assert isinstance(docker_spawner, ride.spawn.DockerSpawner)
    assert isinstance(process_spawner, ride.spawn.ProcessSpawner)
    assert isinstance(spawner._spawners[ride.spawn.SummonLaunchSpec], ride.spawn.SummonSpawner)
    # both attached-capable spawners point at the same per-session host log
    host_log = workspace.host_log
    assert docker_spawner._host_log == host_log
    assert process_spawner._host_log == host_log
    assert set(captured['handlers']) == {'ping', 'started', 'completed', 'summon'}
    assert captured['handlers']['ping'] is ride.spawn.ping_handler
    assert captured['handlers']['completed'] is ride.spawn._log_root_completed
    # the summon handler and the delivery tap belong to the same per-root control
    control = captured['handlers']['summon'].__self__
    assert isinstance(control, ride.summon_control.SummonControl)
    control_observer, child_trail_observer = captured['observers']
    assert control_observer.__self__ is control
    # the second tap publishes summoned children's started trails beside their
    # workspace records, bound to this root's project
    from bro.broker.brotocol import Message, Tag

    child_trail_observer(
      'CH', 'root', Message(type=Tag.STARTED, payload={'trail_id': 't-7'}, in_reply_to='req')
    )
    pointer = trail_pointer.broker_pointer(workspace_dir(tmp_path / 'proj', 'broker-CH'))
    assert trail_pointer.read(pointer) == 't-7'
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
    control = ride.summon_control.SummonControl(
      allow_list=set(),
      workspace=workspace,
      status_file=tmp_path / 'status.json',
      audit_file=tmp_path / 'audit.jsonl',
    )
    ride.spawn._note_root_started(control, workspace)(
      dispatcher, 'root', Message(type=Tag.STARTED, payload={'trail_id': 't-1'})
    )
    # the started handler doubles as the bro-run root's provenance source
    assert control._root_trail_id == 't-1'
    pointer = trail_pointer.broker_pointer(workspace.path)
    assert json.loads(pointer.read_text()) == {'trail_id': 't-1'}
    ride.spawn._log_root_completed(
      dispatcher,
      'root',
      Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'ok'}),
    )
    # a raised run surfaces its reason — the result is the failure cause
    ride.spawn._log_root_completed(
      dispatcher,
      'root',
      Message(type=Tag.COMPLETED, payload={'result': 'no api key', 'end_reason': 'raised'}),
    )
    assert any('root run started (trail t-1)' in record.message for record in caplog.records)
    assert any('root run ended: ok' in record.message for record in caplog.records)
    assert any('root run raised: no api key' in record.message for record in caplog.records)
