import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ride.artifacts
import ride.bro
import ride.do_ride
import ride.harness
import ride.identity
import ride.peer_facts
import ride.scope
import ride.session
import ride.spawn
import ride.summon_control
import ride.workspace.docker as workspace_docker
import ride.workspace.store as workspace_store
from bro.broker.journal import Journal
from bro.broker.transports.tcp import LOCAL_HOST, Endpoint
from bro.monitor import SESSION_DIR_ENV, workspace_session_dir
from bro.workspace.human import HUMAN_EMAIL_ENV, HUMAN_NAME_ENV
from bro.workspace.paths import CONTAINER_SESSION_DIR, summon_dir, workspace_dir, workspace_tree
from ride.runtime_bundle import RuntimeBundle
from ride.workspace.metadata import Isolation
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


def _runtime_bundle(tmp_path: Path) -> RuntimeBundle:
  root = tmp_path / 'runtime' / 'bundle'
  (root / 'host' / 'venv' / 'bin').mkdir(parents=True)
  (root / 'host' / 'bin').mkdir()
  (root / 'host' / '.complete').touch()
  command = root / 'host' / 'venv' / 'bin' / 'do-ride'
  command.write_text('#!/bin/sh\necho unboxed-child-output\n')
  command.chmod(0o755)
  return RuntimeBundle(root, '3.12')


SESSION = 'session-ws'


def _do_ride_environment(workspace_name: str) -> dict[str, str]:
  workspace = Workspace.open(workspace_name)
  spec = ride.session.load_resume_spec(workspace)
  assert spec is not None
  assert workspace.metadata.branch is not None
  return {
    ride.do_ride.INSTALL_DIRECTORY_ENV: ride.do_ride.CONTAINER_INSTALL_DIRECTORY,
    ride.do_ride.RESOLVED_LLM_ENV: ride.do_ride.encode_resolved_llm(spec.resolved_llm),
    'RIDE_ISOLATION': 'boxed',
    'RIDE_BRANCH': workspace.metadata.branch,
  }


def _session_state_mount(workspace_name: str) -> str:
  return f'{workspace_session_dir(workspace_dir(workspace_name))}:{CONTAINER_SESSION_DIR}'


def _lower_boxed(
  launch: ride.spawn.SummonLaunchSpec,
  workspace_name: str,
  container_runtime: workspace_docker.ContainerRuntimeResolver,
  artifacts: ride.artifacts.ArtifactStore,
) -> ride.spawn.DockerLaunchSpec:
  lowered = ride.spawn._lower_summon(
    launch,
    workspace_name,
    MagicMock(spec=RuntimeBundle),
    container_runtime,
    artifacts,
  )
  assert isinstance(lowered, ride.spawn.DockerLaunchSpec)
  return lowered


def _artifacts() -> ride.artifacts.ArtifactStore:
  return ride.artifacts.ArtifactStore(
    Workspace.ensure(SESSION, None, Isolation.BOXED), root_boxed=False
  )


def _facts_expecting(quest: str) -> ride.peer_facts.PeerFacts:
  workspace = Workspace.ensure(SESSION, None, Isolation.BOXED)
  facts = ride.peer_facts.PeerFacts(
    ride.peer_facts.PeerFact('ws', 'bro-dev', frozenset()),
    root_tree=workspace.tree,
    root_path=workspace.path,
  )
  facts.add(quest, ride.peer_facts.PeerFact(None, 'dev', frozenset()))
  return facts


@pytest.fixture
def lowering_harness(monkeypatch, tmp_path):
  def fake_scoped_secrets(
    name, surface, attachment=None, llm_spec=None, grant=(), revoke=(), check_selection=True
  ):
    grant_credentials, _ = ride.scope.split_scope_overrides(list(grant))
    revoke_credentials, _ = ride.scope.split_scope_overrides(list(revoke))
    return workspace_store.finalize_scoped_secrets(
      workspace_store.ScopedSecrets(required={'aws', 'trails'}, optional={'openai'}),
      grant=grant_credentials,
      revoke=revoke_credentials,
    )

  monkeypatch.setattr(ride.spawn, 'scoped_secrets', fake_scoped_secrets)
  monkeypatch.setattr(
    ride.spawn,
    'preflight_scoped_launch',
    lambda scoped, *_args, **_kwargs: (
      set(),
      ride.scope.HydratedStore({}, frozenset(scoped.required | scoped.optional)),
    ),
  )
  monkeypatch.setattr(ride.session, 'local_trails_mounts', lambda scoped: ())
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
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered == ride.spawn.DockerLaunchSpec(
      workspace_docker.Launch(
        name='broker-CH',
        command=[
          'do-ride',
          'solo',
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
          **_do_ride_environment('broker-CH'),
          'RIDE_BRO': 'dev',
          'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness bro dev deploy the thing',
          'RIDE_MAY_SUMMON': '',
          'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
          'RIDE_SUMMONED': '1',
          'RIDE_SUMMONER': '{"session":"ws"}',
        },
        secrets={'aws', 'trails'},
        optional_secrets={'openai'},
        tty=False,
        forward_env=False,
        image='runtime-image',
        runtime_bundle_hash='bundle-hash',
        extra_mounts=(
          _session_state_mount('broker-CH'),
          ride.artifacts.view_mount(SESSION, 'broker-CH'),
        ),
        repo=Path('/proj'),
        base_ref='PARENT-SHA',
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
      harness='bro',
    )
    with caplog.at_level('INFO'):
      _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert 'scoped secrets for summoned dev: aws, trails' in caplog.text

  def test_hold_rides_the_childs_do_ride_argv(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      hold='attended',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command[-4:] == ['--hold', 'attended', 'dev', 'deploy the thing']

  def test_the_llm_recipe_rides_the_childs_do_ride_argv(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      llm='openai:sol:high+fast',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command[-6:] == [
      '--hold', 'unattended', '--llm', 'openai:sol:high+fast', 'dev', 'deploy the thing',
    ]  # fmt: skip

  def test_the_llm_recipe_selects_the_childs_hydrated_llm_key(self, lowering_harness, monkeypatch):
    captured: list = []

    def capture_scope(
      name, recipe, attachment=None, llm_spec=None, grant=(), revoke=(), check_selection=True
    ):
      captured.append(llm_spec)
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.spawn, 'scoped_secrets', capture_scope)
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      llm='echo',
    )
    _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
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
      harness='bro',
      grant=('gmail_creds', '@reviewer'),
      revoke=('openai',),
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
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
      harness='bro',
      grant=('aws',),
    )
    with pytest.raises(ValueError, match='already in the scoped credential set'):
      _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
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
      harness='bro',
      hold='guided',
      llm='openai:sol:high',
      grant=('gmail_creds', '@reviewer'),
      revoke=('openai',),
    )
    _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    workspace = Workspace.open('broker-CH')
    assert workspace.metadata.throwaway
    assert (
      ride.session.load_resume_spec(workspace)
      == ride.session.SessionSpec(
        name='broker-CH',
        harness='bro',
        workspace_pinned=False,
        isolation=Isolation.BOXED,
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
      ride.session, 'local_trails_mounts', lambda scoped: ('/host/trails:/var/ride/trails',)
    )
    monkeypatch.setattr(
      ride.bro.BRO,
      'container_extras',
      lambda spec, workspace, scoped: ride.harness.ContainerExtras(
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
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.extra_mounts == (
      '/host/state:/state',
      '/host/trails:/var/ride/trails',
      _session_state_mount('broker-CH'),
      ride.artifacts.view_mount(SESSION, 'broker-CH'),
    )

  def test_the_child_keeps_session_state_like_any_boxed_session(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert workspace_session_dir(workspace_dir('broker-CH')).is_dir()
    assert lowered.launch.env[SESSION_DIR_ENV] == str(CONTAINER_SESSION_DIR)
    assert _session_state_mount('broker-CH') in lowered.launch.extra_mounts

  def test_the_childs_own_allow_list_rides_its_environment(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=('bro', 'reviewer'),
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
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
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
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
      harness='bro',
      into='summon',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts()).launch
    assert lowered.env == {
      **_do_ride_environment('broker-CH'),
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness bro --into summon dev p',
      'RIDE_MAY_SUMMON': '',
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"session":"ws"}',
    }
    assert lowered.base_ref == 'REF-SHA'

  def test_unresolvable_into_fails_the_spawn(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      into='nope',
    )
    with pytest.raises(ValueError, match='nope'):
      _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())

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
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.base_ref is None
    assert '--repo' not in lowered.launch.command
    assert Workspace.open('broker-CH').repo is None

  @pytest.mark.asyncio
  async def test_unboxed_started_child_keeps_failed_workspace_without_credentials(
    self, lowering_harness, tmp_path
  ):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='detached-root',
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      isolation=Isolation.UNBOXED,
    )
    lowered = ride.spawn._lower_summon(
      launch,
      'broker-CH',
      _runtime_bundle(tmp_path),
      _container_runtime(),
      _artifacts(),
    )
    assert isinstance(lowered, ride.spawn.ProcessLaunchSpec)
    assert lowered.workspace == 'broker-CH'
    assert lowered.cleanup_directory is not None
    private_store = Path(lowered.cleanup_directory)
    assert private_store.is_dir()
    install_directory = Path(lowered.env[ride.do_ride.INSTALL_DIRECTORY_ENV])
    assert install_directory.is_relative_to(private_store)
    scoped_store = Path(lowered.env['BRO_STORE'])
    material = scoped_store / 'creds' / 'aws.cred'
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text('aws-secret')
    hook_probe = """
import os
from pathlib import Path
from bro.base import credentials
store = credentials.Store(credentials.default_registry(), Path(os.environ['BRO_STORE']), {})
exported = credentials.install_hooks(
  credentials.default_registry(),
  {'aws'},
  store,
  Path(os.environ['BRO_INSTALL_DIR']),
  dict(os.environ),
)
assert Path(exported['AWS_SHARED_CREDENTIALS_FILE']).read_text() == 'aws-secret'
print('aws-hook-installed')
raise SystemExit(3)
"""
    lowered = replace(lowered, command=[sys.executable, '-c', hook_probe])
    channel = ride.spawn.Provisioned(
      channel='CH',
      host_endpoint=Endpoint(port=7321, token='tk'),
    )

    handle = await ride.spawn.ProcessSpawner().spawn(lowered, channel, 'X-1')

    assert await handle.wait() == 3
    assert 'aws-hook-installed' in handle.output_tail()
    assert not private_store.exists()
    workspace = Workspace.open('broker-CH')
    assert workspace.isolation is Isolation.UNBOXED
    assert not (workspace.path / 'environment').exists()

  def test_unboxed_lowering_surfaces_private_store_cleanup_failure(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='detached-root',
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      isolation=Isolation.UNBOXED,
    )

    def fail_launch(*_args, **_kwargs):
      raise ValueError('launch failed')

    monkeypatch.setattr(ride.spawn, 'started_party_launch', fail_launch)

    def fail_cleanup(path):
      del path
      raise OSError('cleanup refused')

    monkeypatch.setattr(ride.spawn.shutil, 'rmtree', fail_cleanup)

    with pytest.raises(OSError, match='cleanup refused'):
      ride.spawn._lower_summon(
        launch,
        'broker-CH',
        _runtime_bundle(tmp_path),
        _container_runtime(),
        _artifacts(),
      )

  def test_unreadable_parent_head_fails_the_spawn(self, lowering_harness):
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='gone',
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    with pytest.raises(ValueError, match="summoner's HEAD"):
      _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())

  @pytest.mark.asyncio
  async def test_spawner_lowers_off_loop_and_delegates_to_docker(self, lowering_harness):
    class RecordingDocker(ride.spawn.DockerSpawner):
      def __init__(self):
        self.spawned: list = []

      async def spawn(self, launch, channel, quest):
        self.spawned.append((launch, channel, quest))
        return MagicMock()

    docker = RecordingDocker()
    facts = _facts_expecting('X-1')
    spawner = ride.spawn.SummonSpawner(
      docker,
      ride.spawn.ProcessSpawner(),
      MagicMock(),
      _container_runtime(),
      facts,
      _artifacts(),
    )
    channel = ride.spawn.Provisioned(channel='CH', host_endpoint=Endpoint(port=7321, token='tk'))
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    await spawner.spawn(launch, channel, 'X-1')
    [(lowered, lowered_channel, lowered_quest)] = docker.spawned
    assert isinstance(lowered, ride.spawn.DockerLaunchSpec)
    assert lowered.launch.command == [
      'do-ride', 'solo', '--workspace', 'broker-CH', '--harness', 'bro', '--repo', '/proj',
      '--hold', 'unattended', 'dev', 'p',
    ]  # fmt: skip
    assert lowered.launch.name == 'broker-CH'
    assert lowered_channel is channel
    assert lowered_quest == 'X-1'

  @pytest.mark.asyncio
  async def test_lowering_failure_propagates_out_of_spawn(self, lowering_harness):
    spawner = ride.spawn.SummonSpawner(
      ride.spawn.DockerSpawner(),
      ride.spawn.ProcessSpawner(),
      MagicMock(),
      _container_runtime(),
      _facts_expecting('X-1'),
      _artifacts(),
    )
    channel = ride.spawn.Provisioned(channel='CH', host_endpoint=Endpoint(port=7321, token='tk'))
    launch = ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
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
      lambda spec, workspace, scoped: ride.harness.ContainerExtras(
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
    lowered = _lower_boxed(self._launch(), 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command == [
      'do-ride', 'solo', '--workspace', 'broker-CH', '--harness', 'claude', '--repo', '/proj',
      '--hold', 'unattended', 'dev', 'deploy the thing',
    ]  # fmt: skip
    assert lowered.launch.env == {
      'CLAUDE_CONFIG_DIR': '/home/ride/.claude',
      **_do_ride_environment('broker-CH'),
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness claude dev deploy the thing',
      'RIDE_MAY_SUMMON': '',
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"session":"ws"}',
    }
    assert lowered.launch.base_ref == 'PARENT-SHA'
    assert lowered.launch.extra_mounts == (
      '/host/claude:/home/ride/.claude',
      _session_state_mount('broker-CH'),
      ride.artifacts.view_mount(SESSION, 'broker-CH'),
    )
    assert lowered.launch.tty is False
    assert lowered.launch.forward_env is False

  def test_records_the_claude_resume_spec(self, claude_harness, tmp_path):
    from bro.llm.llms.claude_code import LLMSpec as ClaudeCodeSpec

    _lower_boxed(
      self._launch(llm=':fable5', summon_depth=5, summon_harness='claude'),
      'broker-CH',
      _container_runtime(),
      _artifacts(),
    )
    workspace = Workspace.open('broker-CH')
    spec = ride.session.load_resume_spec(workspace)
    assert spec is not None
    assert spec.harness == 'claude'
    assert spec.harness_options == {'raw': False}
    assert spec.resolved_llm == ClaudeCodeSpec(model='claude-fable-5').dump()
    assert spec.summon_depth == 5
    assert spec.summon_harness == 'claude'

  def test_scope_follows_the_claude_recipe(self, claude_harness, monkeypatch):
    captured: list = []

    def capture_scope(
      name, recipe, attachment=None, llm_spec=None, grant=(), revoke=(), check_selection=True
    ):
      captured.append(recipe.name)
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.spawn, 'scoped_secrets', capture_scope)
    _lower_boxed(self._launch(), 'broker-CH', _container_runtime(), _artifacts())
    assert captured == ['claude-full']

  def test_auth_preflight_failure_fails_the_spawn_before_the_workspace(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    from ride.claude.harness import CLAUDE

    monkeypatch.setattr(CLAUDE, 'preflight_auth', lambda spec: 'claude_code secret not resolvable')
    with pytest.raises(ValueError, match='claude_code secret not resolvable'):
      _lower_boxed(self._launch(), 'broker-CH', _container_runtime(), _artifacts())
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  def test_a_native_recipe_fails_the_claude_spawn(self, claude_harness, tmp_path):
    from bro.llm.providers import LLMSelectionError

    with pytest.raises(LLMSelectionError, match='runs Claude Code, not openai'):
      _lower_boxed(self._launch(llm='openai:sol'), 'broker-CH', _container_runtime(), _artifacts())
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  def test_a_claude_recipe_rides_the_do_ride_argv(self, claude_harness):
    lowered = _lower_boxed(
      self._launch(llm=':fable5:high'), 'broker-CH', _container_runtime(), _artifacts()
    )
    command = lowered.launch.command
    assert command[command.index('--llm') + 1] == ':fable5:high'

  def test_an_explicit_bro_harness_matches_the_default_lowering(self, lowering_harness):
    explicit = _lower_boxed(
      self._launch(harness='bro'), 'broker-CH', _container_runtime(), _artifacts()
    )
    assert explicit.launch.command[:6] == [
      'do-ride', 'solo', '--workspace', 'broker-CH', '--harness', 'bro',
    ]  # fmt: skip


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
        self.journal = Journal()

      def on(self, message_type, handler):
        captured['handlers'][message_type] = handler

      def subscribe(self, observer):
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
    workspace = Workspace.create('ws', tmp_path / 'proj', Isolation.BOXED)
    assert (
      ride.spawn.run_root_via_broker(
        launch,
        workspace=workspace,
        bro='bro-dev',
        summon_depth=4,
        credential_scope=workspace_store.ScopedSecrets({'harbor'}, set()),
        container_runtime=_container_runtime(),
        runtime_bundle=MagicMock(),
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
    control = captured['handlers']['summon'].__self__
    assert isinstance(control, ride.summon_control.SummonControl)
    facts_projection, lifecycle_projection, audit_writer = captured['observers']
    assert isinstance(facts_projection.__self__, ride.peer_facts.PeerFacts)
    assert lifecycle_projection.__self__ is control
    assert audit_writer.__self__ is control
    assert control._workspace is workspace
    assert control._audit_file == summon_dir() / 'ws.jsonl'
    assert control._depth_cap == 4
    assert captured['launch'] is launch
