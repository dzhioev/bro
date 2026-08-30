import json
import subprocess
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

import ride.artifacts
import ride.bro
import ride.identity
import ride.peers
import ride.scope
import ride.session
import ride.spawn
import ride.summon_control
import ride.workspace.docker as workspace_docker
import ride.workspace.store as workspace_store
from bro.broker.dispatcher import Dispatcher
from bro.broker.transports.tcp import LOCAL_HOST, Endpoint
from bro.monitor import trail_pointer
from bro.workspace.human import HUMAN_EMAIL_ENV, HUMAN_NAME_ENV
from bro.workspace.paths import summon_dir, workspace_dir, workspace_tree
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace

PARENT = 'parent'
SUMMONER = {'session': 'ws'}
# the lowering harness stubs the human-identity read; the test that asserts on
# the real one restores it
_REAL_HUMAN_IDENTITY = ride.identity.human_git_identity_env


def _container_runtime() -> workspace_docker.ContainerRuntimeResolver:
  return workspace_docker.ContainerRuntimeResolver.fixed(
    workspace_docker.ContainerRuntime('runtime-image', 'bundle-hash')
  )


SESSION = 'session-ws'


def _artifacts() -> ride.artifacts.ArtifactStore:
  return ride.artifacts.ArtifactStore(
    Workspace.ensure(SESSION, None, WorkspaceKind.CONTAINER), root_in_container=False
  )


@dataclass
class _Context:
  """the two Dispatcher facts peer attribution reads."""

  root: str = 'ROOT'
  workers: dict = dataclass_field(default_factory=dict)


def _peers_expecting(exchange: str) -> ride.peers.Peers:
  peers = ride.peers.Peers(Workspace.ensure(SESSION, None, WorkspaceKind.CONTAINER))
  peers.note_summon(cast(Dispatcher, _Context()), 'ROOT', exchange)
  return peers


@pytest.fixture
def lowering_harness(monkeypatch, tmp_path):
  monkeypatch.setattr(
    ride.scope,
    'scoped_secrets',
    lambda name, surface, attachment=None, llm_spec=None: workspace_store.ScopedSecrets(
      required={'aws', 'trails'}, optional={'openai'}
    ),
  )
  monkeypatch.setattr(ride.spawn, 'local_trails_mounts', lambda scoped: ())
  monkeypatch.setattr(ride.spawn, 'human_git_identity_env', lambda repository: {})
  monkeypatch.setattr(
    ride.spawn,
    'resolve_head',
    lambda root, repository: 'PARENT-SHA' if repository == workspace_tree(PARENT) else None,
  )
  monkeypatch.setattr(
    ride.spawn, 'resolve_ref', lambda root, ref: 'REF-SHA' if ref == 'summon' else None
  )


class TestSummonLowering:
  def test_lowers_to_the_bro_run_docker_launch(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered == ride.spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='broker-CH',
        command=[
          'ride',
          'solo',
          '--in-place',
          '--workspace',
          'broker-CH',
          '--harness',
          'bro',
          '--repo',
          '/proj',
          '--hold',
          'unattended',
          'dev',
          'deploy the thing',
        ],  # fmt: skip
        env={
          'RIDE_BASE_REF': 'PARENT-SHA',
          'RIDE_BRO': 'dev',
          'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness bro dev deploy the thing',
          'RIDE_MAY_SUMMON': '',
          'RIDE_SUMMONED': '1',
          'RIDE_SUMMONER': '{"session":"ws"}',
        },
        secrets={'aws', 'trails'},
        optional_secrets={'openai'},
        tty=False,
        forward_env=False,
        image='runtime-image',
        runtime_bundle_hash='bundle-hash',
        extra_mounts=(ride.artifacts.view_mount(SESSION, 'broker-CH'),),
        repo=Path('/proj'),
      ),
    )

  def test_lowering_logs_the_scope_like_any_container_launch(self, lowering_harness, caplog):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
    )
    with caplog.at_level('INFO'):
      ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert 'scoped secrets for summoned dev: aws, trails' in caplog.text

  def test_hold_rides_the_childs_inner_argv(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      hold='attended',
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command[-4:] == ['--hold', 'attended', 'dev', 'deploy the thing']

  def test_the_llm_recipe_rides_the_childs_inner_argv(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      llm='openai:sol:high+fast',
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command[-6:] == [
      '--hold', 'unattended', '--llm', 'openai:sol:high+fast', 'dev', 'deploy the thing',
    ]  # fmt: skip

  def test_the_llm_recipe_selects_the_childs_hydrated_llm_key(self, lowering_harness, monkeypatch):
    captured: list = []

    def capture_scope(name, recipe, attachment=None, llm_spec=None):
      captured.append(llm_spec)
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.scope, 'scoped_secrets', capture_scope)
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      llm='echo',
    )
    ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert captured == [ride.bro.BRO.resolve_llm('echo', 'dev')]

  def test_credential_overrides_adjust_the_childs_scope(self, lowering_harness):
    # only the credential halves reach the scope; the `@bro` half was already
    # resolved into may_summon by the control
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      grant=('gmail_creds', '@reviewer'),
      revoke=('openai',),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.secrets == {'aws', 'trails', 'gmail_creds'}
    assert lowered.launch.optional_secrets == set()

  def test_no_op_credential_override_fails_the_spawn(self, lowering_harness, tmp_path):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      grant=('aws',),
    )
    with pytest.raises(ValueError, match='already in the scoped credential set'):
      ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    # every fallible resolution precedes the workspace record, so nothing to
    # reclaim is left behind
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  def test_lowering_records_the_childs_resume_spec(self, lowering_harness, tmp_path):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      hold='guided',
      llm='openai:sol:high',
      grant=('gmail_creds', '@reviewer'),
      revoke=('openai',),
    )
    ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    workspace = Workspace.open('broker-CH')
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
        repo='/proj',
      ).resume_variant()
    )

  def test_launch_mounts_carry_harness_extras_and_local_trails(self, lowering_harness, monkeypatch):
    monkeypatch.setattr(
      ride.spawn, 'local_trails_mounts', lambda scoped: ('/host/trails:/var/ride/trails',)
    )
    monkeypatch.setattr(
      ride.bro.BRO,
      'container_extras',
      lambda spec, workspace, scoped: ride.spawn.ContainerExtras(
        env={}, mounts=('/host/state:/state',)
      ),
    )
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.extra_mounts == (
      '/host/state:/state',
      '/host/trails:/var/ride/trails',
      ride.artifacts.view_mount(SESSION, 'broker-CH'),
    )

  def test_the_childs_own_allow_list_rides_its_environment(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=('bro', 'reviewer'),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.env['RIDE_MAY_SUMMON'] == 'bro,reviewer'

  def test_the_child_credits_the_human_of_the_repository_it_shares(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    monkeypatch.setattr(ride.spawn, 'human_git_identity_env', _REAL_HUMAN_IDENTITY)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'absent-global'))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'absent-system'))
    repository = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(repository)], check=True)
    for key, value in (('user.name', 'Ada Lovelace'), ('user.email', 'ada@example.com')):
      subprocess.run(['git', 'config', key, value], cwd=repository, check=True)
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=repository,
      summoner=SUMMONER,
      may_summon=(),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.env[HUMAN_NAME_ENV] == 'Ada Lovelace'
    assert lowered.launch.env[HUMAN_EMAIL_ENV] == 'ada@example.com'

  def test_into_overrides_the_inherited_base_ref(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      into='summon',
    )
    assert ride.spawn._lower_summon(
      launch, 'broker-CH', _container_runtime(), _artifacts()
    ).launch.env == {
      'RIDE_BASE_REF': 'REF-SHA',
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness bro --into summon dev p',
      'RIDE_MAY_SUMMON': '',
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"session":"ws"}',
    }

  def test_unresolvable_into_fails_the_spawn(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      into='nope',
    )
    with pytest.raises(ValueError, match='nope'):
      ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())

  def test_detached_root_spawns_a_detached_child_without_reading_git(
    self, lowering_harness, monkeypatch
  ):
    monkeypatch.setattr(
      ride.spawn, 'resolve_head', lambda root, repository: pytest.fail('git must not be read')
    )
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='empty',
      summoner=SUMMONER,
      may_summon=(),
    )
    lowered = ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert 'RIDE_BASE_REF' not in lowered.launch.env
    assert '--repo' not in lowered.launch.command
    assert Workspace.open('broker-CH').repo is None

  def test_unreadable_parent_head_fails_the_spawn(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='gone',
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
    )
    with pytest.raises(ValueError, match="summoner's HEAD"):
      ride.spawn._lower_summon(launch, 'broker-CH', _container_runtime(), _artifacts())

  @pytest.mark.asyncio
  async def test_spawner_lowers_off_loop_and_delegates_to_docker(self, lowering_harness):
    class RecordingDocker(ride.spawn.DockerSpawner):
      def __init__(self):
        self.spawned: list = []

      async def spawn(self, launch, channel, exchange):
        self.spawned.append((launch, channel, exchange))
        return MagicMock()

    docker = RecordingDocker()
    peers = _peers_expecting('X-1')
    spawner = ride.spawn.SummonSpawner(docker, _container_runtime(), peers, _artifacts())
    channel = ride.spawn.Provisioned(channel='CH', host_endpoint=Endpoint(port=7321, token='tk'))
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
    )
    await spawner.spawn(launch, channel, 'X-1')
    [(lowered, lowered_channel, lowered_exchange)] = docker.spawned
    assert isinstance(lowered, ride.spawn.DockerLaunchSpec)
    assert lowered.launch.command == [
      'ride', 'solo', '--in-place', '--workspace', 'broker-CH', '--harness', 'bro', '--repo', '/proj',
      '--hold', 'unattended', 'dev', 'p',
    ]  # fmt: skip
    assert lowered.launch.name == 'broker-CH'
    assert lowered_channel is channel
    assert lowered_exchange == 'X-1'

  @pytest.mark.asyncio
  async def test_lowering_failure_propagates_out_of_spawn(self, lowering_harness):
    spawner = ride.spawn.SummonSpawner(
      ride.spawn.DockerSpawner(), _container_runtime(), _peers_expecting('X-1'), _artifacts()
    )
    channel = ride.spawn.Provisioned(channel='CH', host_endpoint=Endpoint(port=7321, token='tk'))
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      into='nope',
    )
    # the raise crosses to_thread back onto the loop: Dispatcher.spawn turns it
    # into the correlated failed{reason: 'launch'}
    with pytest.raises(ValueError, match='nope'):
      await spawner.spawn(launch, channel, 'X-1')


class TestClaudeSummonLowering:
  @pytest.fixture
  def claude_harness(self, lowering_harness, monkeypatch):
    from ride.claude.harness import CLAUDE

    monkeypatch.setattr(CLAUDE, 'preflight_auth', lambda spec: None)
    monkeypatch.setattr(
      CLAUDE,
      'container_extras',
      lambda spec, workspace, scoped: ride.spawn.ContainerExtras(
        env={'CLAUDE_CONFIG_DIR': '/home/ride/.claude'},
        mounts=('/host/claude:/home/ride/.claude',),
      ),
    )

  def _launch(self, **overrides) -> ride.spawn.SummonLaunchSpec:
    fields: dict = {
      'target': 'dev',
      'prompt': 'deploy the thing',
      'parent': PARENT,
      'repo': Path('/proj'),
      'summoner': SUMMONER,
      'may_summon': (),
      'harness': 'claude',
      **overrides,
    }
    return ride.spawn.SummonLaunchSpec(**fields)

  def test_lowers_to_a_ride_solo_claude_launch(self, claude_harness):
    lowered = ride.spawn._lower_summon(
      self._launch(), 'broker-CH', _container_runtime(), _artifacts()
    )
    assert lowered.launch.command == [
      'ride', 'solo', '--in-place', '--workspace', 'broker-CH', '--harness', 'claude', '--repo', '/proj',
      '--hold', 'unattended', 'dev', 'deploy the thing',
    ]  # fmt: skip
    assert lowered.launch.env == {
      'CLAUDE_CONFIG_DIR': '/home/ride/.claude',
      'RIDE_BASE_REF': 'PARENT-SHA',
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness claude dev deploy the thing',
      'RIDE_MAY_SUMMON': '',
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"session":"ws"}',
    }
    assert lowered.launch.extra_mounts == (
      '/host/claude:/home/ride/.claude',
      ride.artifacts.view_mount(SESSION, 'broker-CH'),
    )
    assert lowered.launch.tty is False
    assert lowered.launch.forward_env is False

  def test_records_the_claude_resume_spec(self, claude_harness, tmp_path):
    from bro.llm.llms.claude_code import LLMSpec as ClaudeCodeSpec

    ride.spawn._lower_summon(
      self._launch(llm=':fable5'), 'broker-CH', _container_runtime(), _artifacts()
    )
    workspace = Workspace.open('broker-CH')
    spec = ride.session.load_resume_spec(workspace)
    assert spec is not None
    assert spec.harness == 'claude'
    assert spec.harness_options == {'raw': False}
    assert spec.resolved_llm == ClaudeCodeSpec(model='claude-fable-5').dump()

  def test_scope_follows_the_claude_recipe(self, claude_harness, monkeypatch):
    captured: list = []

    def capture_scope(name, recipe, attachment=None, llm_spec=None):
      captured.append(recipe.name)
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.scope, 'scoped_secrets', capture_scope)
    ride.spawn._lower_summon(self._launch(), 'broker-CH', _container_runtime(), _artifacts())
    assert captured == ['claude-full']

  def test_auth_preflight_failure_fails_the_spawn_before_the_workspace(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    from ride.claude.harness import CLAUDE

    monkeypatch.setattr(CLAUDE, 'preflight_auth', lambda spec: 'claude_code secret not resolvable')
    with pytest.raises(ValueError, match='claude_code secret not resolvable'):
      ride.spawn._lower_summon(self._launch(), 'broker-CH', _container_runtime(), _artifacts())
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  def test_a_native_recipe_fails_the_claude_spawn(self, claude_harness, tmp_path):
    from bro.llm.providers import LLMSelectionError

    with pytest.raises(LLMSelectionError, match='runs Claude Code, not openai'):
      ride.spawn._lower_summon(
        self._launch(llm='openai:sol'), 'broker-CH', _container_runtime(), _artifacts()
      )
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  def test_a_claude_recipe_rides_the_inner_argv(self, claude_harness):
    lowered = ride.spawn._lower_summon(
      self._launch(llm=':fable5:high'), 'broker-CH', _container_runtime(), _artifacts()
    )
    command = lowered.launch.command
    assert command[command.index('--llm') + 1] == ':fable5:high'

  def test_an_explicit_bro_harness_matches_the_default_lowering(self, lowering_harness):
    explicit = ride.spawn._lower_summon(
      self._launch(harness='bro'), 'broker-CH', _container_runtime(), _artifacts()
    )
    assert explicit.launch.command[:7] == [
      'ride', 'solo', '--in-place', '--workspace', 'broker-CH', '--harness', 'bro',
    ]  # fmt: skip


class TestChildTrailPublication:
  def test_started_delivery_publishes_the_childs_session_pointer(self, tmp_path):
    from bro.broker import brotocol

    observe = ride.spawn._note_child_started(_peers_expecting('req'))
    observe('CH', 'root', brotocol.progress('req', {'trail_id': 't-9'}))
    pointer = trail_pointer.session_pointer(workspace_dir('broker-CH'))
    assert trail_pointer.read(pointer) == 't-9'

  def test_a_registered_childs_pointer_lands_under_its_recorded_workspace(self, tmp_path):
    from bro.broker import brotocol

    peers = _peers_expecting('req')
    peers.note_workspace('req', 'my-manual')
    observe = ride.spawn._note_child_started(peers)
    observe('CH', 'root', brotocol.progress('req', {'trail_id': 't-9'}))
    pointer = trail_pointer.session_pointer(workspace_dir('my-manual'))
    assert trail_pointer.read(pointer) == 't-9'

  def test_non_started_and_host_anchored_deliveries_publish_nothing(self, tmp_path):
    from bro.broker import brotocol

    observe = ride.spawn._note_child_started(_peers_expecting('r'))
    observe('CH', 'root', brotocol.result('r', 'ok', value='t'))
    observe(None, 'root', brotocol.progress('r', {'trail_id': 't'}))  # launch-failure synthesis
    observe('CH', None, brotocol.progress('r', {'trail_id': 't'}))  # the root's own started
    observe('CH', 'root', brotocol.progress('r', {}))
    assert not trail_pointer.session_pointer(workspace_dir('broker-CH')).exists()


class TestBrokerBindHosts:
  """the gateway branch that binds is what the `broker_e2e` stage exercises for
  real; these pin the two ways it falls back to loopback alone."""

  def test_a_host_with_no_docker_bridge_binds_loopback_alone(self, monkeypatch):
    monkeypatch.setattr(ride.spawn, 'bridge_gateway', lambda: None)
    assert ride.spawn.broker_bind_hosts() == [LOCAL_HOST]

  def test_a_gateway_that_is_no_address_here_binds_loopback_alone(self, monkeypatch):
    # what a daemon in a VM reports: its own gateway, which this host cannot bind
    monkeypatch.setattr(ride.spawn, 'bridge_gateway', lambda: '192.0.2.1')
    assert ride.spawn.broker_bind_hosts() == [LOCAL_HOST]


class TestRunRootViaBroker:
  def test_wires_bind_hosts_composite_spawner_handlers_and_run(self, monkeypatch, tmp_path):
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

    def contributed_handler(context, peer, message):
      del context, peer, message

    kind_contexts: list = []

    def fake_extension_kinds(context):
      kind_contexts.append(context)
      return {'contributed': contributed_handler}

    monkeypatch.setattr(ride.spawn, 'extension_kinds', fake_extension_kinds)
    launch = ride.spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    workspace = Workspace.create('ws', tmp_path / 'proj', WorkspaceKind.CONTAINER)
    assert (
      ride.spawn.run_root_via_broker(
        launch,
        workspace=workspace,
        credential_scope=workspace_store.ScopedSecrets({'harbor'}, set()),
        container_runtime=_container_runtime(),
      )
      == 3
    )
    assert captured['transport']._bind_hosts[0] == LOCAL_HOST
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
    assert set(captured['handlers']) == {
      'ping',
      'summon',
      'artifact.mint',
      'artifact.get',
      'contributed',
    }
    assert captured['handlers']['ping'] is ride.spawn.ping_handler
    # installed distributions' kinds register beside the built-ins, built for
    # this session's workspace tree
    assert captured['handlers']['contributed'] is contributed_handler
    [kind_context] = kind_contexts
    assert kind_context.workspace_tree == workspace.tree
    assert isinstance(kind_context.artifacts, ride.artifacts.ArtifactControl)
    assert kind_context.credential_scope == frozenset({'harbor'})
    # the summon handler and the delivery tap belong to the same per-root control
    control = captured['handlers']['summon'].__self__
    assert isinstance(control, ride.summon_control.SummonControl)
    control_observer, child_trail_observer, _root_observer = captured['observers']
    assert control_observer.__self__ is control
    # the second tap publishes summoned children's started trails beside their
    # workspace records, bound to this root's project
    from bro.broker import brotocol

    child_trail_observer('CH', 'root', brotocol.progress('req', {'trail_id': 't-7'}))
    pointer = trail_pointer.session_pointer(workspace_dir('broker-CH'))
    assert trail_pointer.read(pointer) == 't-7'
    assert control._workspace is workspace
    state_directory = summon_dir()
    assert control._status_file == state_directory / 'ws.status.json'
    assert control._audit_file == state_directory / 'ws.jsonl'
    assert captured['launch'] is launch

  def test_root_lifecycle_observer_logs_trail_and_outcome(self, caplog, tmp_path):
    from bro.broker import brotocol

    workspace = Workspace.create('ws', tmp_path, WorkspaceKind.CONTAINER)
    control = ride.summon_control.SummonControl(
      allow_list=set(),
      credential_scope=workspace_store.ScopedSecrets(set(), set()),
      workspace=workspace,
      peers=ride.peers.Peers(workspace),
      artifacts=ride.artifacts.ArtifactStore(workspace, root_in_container=False),
      status_file=tmp_path / 'status.json',
      audit_file=tmp_path / 'audit.jsonl',
    )
    observe = ride.spawn._root_lifecycle(control, workspace)
    observe('root', None, brotocol.progress('X', {'trail_id': 't-1'}))
    # the started progress doubles as the bro-run root's provenance source
    assert control._root_trail_id == 't-1'
    pointer = trail_pointer.session_pointer(workspace.path)
    assert json.loads(pointer.read_text()) == {'trail_id': 't-1'}
    observe('root', None, brotocol.result('X', 'ok', value='fine'))
    # a raised run surfaces its reason — the error is the failure cause
    observe(
      'root',
      None,
      brotocol.result('X', 'failed', error='no api key', detail={'reason': 'raised'}),
    )
    # a child delivery has a target peer and is not the root's to log
    observe('CH', 'root', brotocol.progress('req', {'trail_id': 't-2'}))
    assert any('root run started (trail t-1)' in record.message for record in caplog.records)
    assert any('root run ended: ok' in record.message for record in caplog.records)
    assert any('root run raised: no api key' in record.message for record in caplog.records)
    assert not any('t-2' in record.message for record in caplog.records)
