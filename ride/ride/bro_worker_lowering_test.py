import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ride.artifacts
import ride.bro
import ride.bro_worker
import ride.broker_root
import ride.do_ride
import ride.harness
import ride.identity
import ride.peer_facts
import ride.scope
import ride.session
import ride.workspace.docker as workspace_docker
import ride.workspace.spawn as workspace_spawn
import ride.workspace.store as workspace_store
from bro.broker.journal import Journal
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import LOCAL_HOST, Endpoint
from bro.monitor import (
  SESSION_DIR_ENV,
  party_member_dir,
  workspace_party_dir,
  workspace_session_dir,
)
from bro.workspace.human import HUMAN_EMAIL_ENV, HUMAN_NAME_ENV
from bro.workspace.paths import (
  CONTAINER_PARTY_DIR,
  CONTAINER_SESSION_DIR,
  workspace_dir,
  workspace_tree,
)
from ride.runtime_bundle import RuntimeBundle
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace

PARENT = 'parent'
SUMMONER = {'trail_id': 'T-parent'}
# the lowering harness stubs the human-identity read; the test that asserts on
# the real one restores it
_REAL_HUMAN_IDENTITY = ride.identity.human_git_identity_env


def _container_runtime() -> workspace_docker.ContainerRuntimeResolver:
  return workspace_docker.ContainerRuntimeResolver.fixed(
    workspace_docker.ContainerRuntime('runtime-image', 'bundle-hash', 'runtime-image')
  )


def _runtime_bundle(tmp_path: Path) -> RuntimeBundle:
  root = tmp_path / 'runtime' / 'bundle'
  (root / 'host' / 'venv' / 'bin').mkdir(parents=True)
  (root / 'host' / 'bin').mkdir()
  (root / 'host' / '.complete').touch()
  command = root / 'host' / 'venv' / 'bin' / 'do-ride'
  command.write_text('#!/bin/sh\necho unboxed-child-output\n')
  command.chmod(0o755)
  broxy = shutil.which('broxy')
  assert broxy is not None
  shutil.copy2(broxy, root / 'host' / 'bin' / 'broxy')
  return RuntimeBundle(root, '3.12')


SESSION = 'session-ws'


def _do_ride_environment(workspace_name: str, base_sha: str = 'PARENT-SHA') -> dict[str, str]:
  workspace = Workspace.open(workspace_name)
  spec = ride.session.load_resume_spec(workspace)
  assert spec is not None
  assert workspace.metadata.branch is not None
  return {
    ride.do_ride.INSTALL_DIRECTORY_ENV: ride.do_ride.CONTAINER_INSTALL_DIRECTORY,
    ride.do_ride.RESOLVED_LLM_ENV: ride.do_ride.encode_resolved_llm(spec.resolved_llm),
    'RIDE_ISOLATION': 'boxed',
    'RIDE_BRANCH': workspace.metadata.branch,
    'RIDE_BASE_SHA': base_sha,
    'RIDE_RUNTIME': '/runtime',
  }


def _session_state_mount(workspace_name: str) -> str:
  return f'{workspace_session_dir(workspace_dir(workspace_name))}:{CONTAINER_SESSION_DIR}'


def _party_mount(workspace_name: str) -> str:
  return f'{workspace_party_dir(workspace_dir(workspace_name))}:{CONTAINER_PARTY_DIR}'


def _lower_boxed(
  launch: ride.bro_worker.SummonLaunchSpec,
  workspace_name: str,
  container_runtime: workspace_docker.ContainerRuntimeResolver,
  artifacts: ride.artifacts.ArtifactStore,
) -> workspace_spawn.DockerLaunchSpec:
  runtime_bundle = MagicMock(spec=RuntimeBundle)
  runtime_bundle.host_root = Path('/runtime')
  runtime_bundle.recorded_reference = None
  lowered = ride.bro_worker._lower_summon(
    launch,
    workspace_name,
    runtime_bundle,
    container_runtime,
    artifacts,
  )
  assert isinstance(lowered, workspace_spawn.DockerLaunchSpec)
  return lowered


def _artifacts() -> ride.artifacts.ArtifactStore:
  return ride.artifacts.ArtifactStore(
    Workspace.ensure(SESSION, None, Isolation.BOXED), root_boxed=False
  )


def _facts_expecting(quest: str) -> ride.peer_facts.PeerFacts:
  workspace = Workspace.ensure(SESSION, None, Isolation.BOXED)
  facts = ride.peer_facts.PeerFacts(
    ride.peer_facts.WorkerFacts(
      'bro',
      'ws',
      extension=ride.bro_worker.BroFacts('bro-dev', frozenset()),
    ),
    root_tree=workspace.tree,
    root_path=workspace.path,
  )
  facts.add(
    quest,
    ride.peer_facts.WorkerFacts(
      'bro',
      None,
      expected=True,
      extension=ride.bro_worker.BroFacts('dev', frozenset()),
    ),
  )
  return facts


@pytest.fixture
def lowering_harness(monkeypatch, tmp_path):
  def fake_scoped_secrets(
    name,
    surface,
    attachment=None,
    attachment_repository=None,
    llm_spec=None,
    grant=(),
    revoke=(),
    check_selection=True,
  ):
    grant_credentials, _, _ = ride.scope.split_scope_overrides(grant)
    revoke_credentials, _, _ = ride.scope.split_scope_overrides(revoke)
    return workspace_store.finalize_scoped_secrets(
      workspace_store.ScopedSecrets(required={'aws', 'trails'}, optional={'openai'}),
      grant=grant_credentials,
      revoke=revoke_credentials,
    )

  monkeypatch.setattr(ride.bro_worker, 'scoped_secrets', fake_scoped_secrets)
  monkeypatch.setattr(
    ride.bro_worker,
    'preflight_scoped_launch',
    lambda scoped, *_args, **_kwargs: (
      set(),
      {'bro.party.start.boxed'},
      ride.scope.HydratedStore({}, frozenset(scoped.required | scoped.optional)),
    ),
  )
  monkeypatch.setattr(ride.session, 'local_trails_mounts', lambda scoped: ())
  monkeypatch.setattr(ride.bro_worker, 'human_git_identity_env', lambda repository: {})
  monkeypatch.setattr(
    ride.bro_worker,
    'resolve_head',
    lambda root, repository: 'PARENT-SHA' if repository == workspace_tree(PARENT) else None,
  )
  monkeypatch.setattr(
    ride.bro_worker, 'resolve_ref', lambda root, ref: 'REF-SHA' if ref == 'summon' else None
  )


class TestSummonLowering:
  def test_lowers_to_the_bro_run_docker_launch(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered == workspace_spawn.DockerLaunchSpec(
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
          'RIDE_PERMITS': 'bro.party.start.boxed',
          'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
          'RIDE_SUMMONED': '1',
          'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
        },
        secrets={'aws', 'trails'},
        optional_secrets={'openai'},
        tty=False,
        image='runtime-image',
        runtime_bundle_hash='bundle-hash',
        extra_mounts=(
          _session_state_mount('broker-CH'),
          _party_mount('broker-CH'),
          ride.artifacts.view_mount(SESSION, 'broker-CH'),
        ),
        repo=Path('/proj'),
        base_ref='PARENT-SHA',
      ),
    )

  def test_lowering_logs_the_scope_like_any_container_launch(self, lowering_harness, caplog):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    with caplog.at_level('INFO'):
      _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert 'scoped secrets for summoned dev: aws, trails' in caplog.text

  def test_hold_rides_the_childs_do_ride_argv(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      hold='attended',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command[-4:] == ['--hold', 'attended', 'dev', 'deploy the thing']

  def test_the_llm_recipe_rides_the_childs_do_ride_argv(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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

  def test_the_hosts_per_bro_recipe_settles_the_childs_llm(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps({'projects': {'/proj': {'bros': {'dev': {'llm': 'openai:sol:xhigh'}}}}})
    )
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      llm='::low',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    command = lowered.launch.command
    assert command[command.index('--llm') + 1] == 'openai:sol:low'
    spec = ride.session.load_resume_spec(Workspace.open('broker-CH'))
    assert spec is not None
    assert spec.llm == 'openai:sol:low'
    assert spec.resolved_llm == ride.bro.BRO.resolve_llm('openai:sol:low', 'dev').dump()

  def test_the_llm_recipe_selects_the_childs_hydrated_llm_key(self, lowering_harness, monkeypatch):
    captured: list = []

    def capture_scope(
      name,
      recipe,
      attachment=None,
      attachment_repository=None,
      llm_spec=None,
      grant=(),
      revoke=(),
      check_selection=True,
    ):
      captured.append(llm_spec)
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.bro_worker, 'scoped_secrets', capture_scope)
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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

  def test_child_resume_record_carries_a_given_runtime(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      isolation=Isolation.UNBOXED,
    )
    runtime_bundle = MagicMock(spec=RuntimeBundle)
    runtime_bundle.recorded_reference = '/given-runtime'
    runtime_bundle.host_root = Path('/given-runtime')
    runtime_bundle.host_venv = Path('/given-runtime/venv')
    runtime_bundle.host_session_env.return_value = {}
    lowered = ride.bro_worker._lower_summon(
      launch,
      'broker-CH',
      runtime_bundle,
      _container_runtime(),
      _artifacts(),
    )
    assert isinstance(lowered, workspace_spawn.ProcessLaunchSpec)
    spec = ride.session.load_resume_spec(Workspace.open('broker-CH'))
    assert spec is not None
    assert spec.runtime_bundle == '/given-runtime'
    assert lowered.cleanup_directory is not None
    shutil.rmtree(lowered.cleanup_directory)

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
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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
      _party_mount('broker-CH'),
      ride.artifacts.view_mount(SESSION, 'broker-CH'),
    )

  def test_the_child_keeps_session_state_like_any_boxed_session(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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
    monkeypatch.setattr(ride.bro_worker, 'human_git_identity_env', _REAL_HUMAN_IDENTITY)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'absent-global'))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'absent-system'))
    repository = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(repository)], check=True)
    for key, value in (('user.name', 'Ada Lovelace'), ('user.email', 'ada@example.com')):
      subprocess.run(['git', 'config', key, value], cwd=repository, check=True)
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=repository,
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.env[HUMAN_NAME_ENV] == 'Ada Lovelace'
    assert lowered.launch.env[HUMAN_EMAIL_ENV] == 'ada@example.com'

  def test_into_overrides_the_inherited_base_ref(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      into='summon',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts()).launch
    assert lowered.env == {
      **_do_ride_environment('broker-CH', base_sha='REF-SHA'),
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness bro --into summon dev p',
      'RIDE_MAY_SUMMON': '',
      'RIDE_PERMITS': 'bro.party.start.boxed',
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
    }
    assert lowered.base_ref == 'REF-SHA'

  def test_unresolvable_into_fails_the_spawn(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
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
      ride.bro_worker, 'resolve_head', lambda root, repository: pytest.fail('git must not be read')
    )
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='empty',
      parent_tree=workspace_tree('empty'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.base_ref is None
    assert '--repo' not in lowered.launch.command
    assert Workspace.open('broker-CH').repo is None

  def test_unboxed_join_runs_in_the_party_tree_with_member_records(
    self, lowering_harness, tmp_path
  ):
    workspace = Workspace.ensure(PARENT, None, Isolation.UNBOXED)
    workspace.tree.mkdir(parents=True)
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='work beside me',
      parent=PARENT,
      parent_tree=workspace.tree,
      summoner=SUMMONER,
      may_summon=('reviewer',),
      permits=('bro.party.join',),
      harness='bro',
      party='join',
      isolation=None,
      env={'IS_SANDBOX': '1'},
    )
    artifacts = _artifacts()

    lowered = ride.bro_worker._lower_join(
      launch,
      'broker-CH',
      _runtime_bundle(tmp_path),
      artifacts,
    )

    assert isinstance(lowered, workspace_spawn.ProcessLaunchSpec)
    records = party_member_dir(workspace.path, 'broker-CH')
    assert lowered.cwd == str(workspace.tree)
    assert lowered.workspace is None
    assert lowered.party_workspace == workspace.name
    assert lowered.records_directory == str(records)
    assert records.is_dir()
    assert lowered.env[SESSION_DIR_ENV] == str(records / 'session')
    assert lowered.env['RIDE_PARTY_MEMBER'] == 'broker-CH'
    assert lowered.env['RIDE_COMMAND'].startswith('summon --join')
    assert lowered.env['RIDE_MAY_SUMMON'] == 'reviewer'
    assert lowered.env['RIDE_PERMITS'] == 'bro.party.join'
    assert lowered.env['RIDE_SUMMONED'] == '1'
    assert lowered.env['IS_SANDBOX'] == '1'
    assert lowered.cleanup_directory is not None
    shutil.rmtree(lowered.cleanup_directory)
    shutil.rmtree(records)

  def _boxed_join(
    self, monkeypatch, tmp_path, env: dict[str, str] | None = None
  ) -> workspace_spawn.ExecLaunchSpec:
    workspace = Workspace.ensure(PARENT, None, Isolation.BOXED)
    monkeypatch.setattr(ride.session, 'find_container_id', lambda tree: 'cid-party')
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='work beside me',
      parent=PARENT,
      parent_tree=workspace.tree,
      summoner=SUMMONER,
      may_summon=('reviewer',),
      permits=('bro.party.join',),
      harness='bro',
      party='join',
      isolation=None,
      env=env if env is not None else {},
    )
    runtime_bundle = MagicMock(spec=RuntimeBundle)
    runtime_bundle.host_root = Path('/runtime')
    runtime_bundle.recorded_reference = None
    lowered = ride.bro_worker._lower_join(launch, 'broker-CH', runtime_bundle, _artifacts())
    assert isinstance(lowered, workspace_spawn.ExecLaunchSpec)
    return lowered

  def test_the_partys_env_additions_reach_a_boxed_member(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    lowered = self._boxed_join(monkeypatch, tmp_path, env={'IS_SANDBOX': '1', 'HOME': '/x'})
    assert lowered.launch.env['IS_SANDBOX'] == '1'
    assert lowered.launch.env['HOME'] == workspace_docker.MEMBER_BASELINE_ENV['HOME']

  def test_boxed_join_lowers_to_a_member_exec(self, lowering_harness, monkeypatch, tmp_path):
    lowered = self._boxed_join(monkeypatch, tmp_path)
    workspace = Workspace.open(PARENT)
    records = party_member_dir(workspace.path, 'broker-CH')
    assert lowered.party_workspace == PARENT
    assert lowered.records_directory == str(records)
    assert workspace_session_dir(records).is_dir()
    member = lowered.launch
    assert member.container == 'cid-party'
    assert member.member == 'broker-CH'
    assert member.command == [
      'do-ride', 'solo', '--workspace', PARENT, '--harness', 'bro',
      '--hold', 'unattended', 'dev', 'work beside me',
    ]  # fmt: skip
    assert member.secrets == {'aws', 'trails'}
    assert member.optional_secrets == {'openai'}
    ride_command = 'summon --join --harness bro dev work beside me'
    resolved = ride.harness.get_harness('bro').resolve_llm(None, 'dev')
    assert member.env == {
      **workspace_docker.MEMBER_BASELINE_ENV,
      'RIDE_BRO': 'dev',
      'RIDE_WORKSPACE': PARENT,
      'RIDE_HOST_WORKSPACE': str(workspace.tree),
      'RIDE_HOST': socket.gethostname(),
      'RIDE_ISOLATION': 'boxed',
      ride.do_ride.RESOLVED_LLM_ENV: ride.do_ride.encode_resolved_llm(resolved.dump()),
      ride.do_ride.INSTALL_DIRECTORY_ENV: '/home/ride/.bro-party/broker-CH/environment',
      SESSION_DIR_ENV: '/var/ride/party/broker-CH/session',
      'RIDE_TRAILS_ROOT': '/var/ride/party/broker-CH/trails',
      'RIDE_RUNTIME': '/runtime',
      'RIDE_COMMAND': ride_command,
      'RIDE_PARTY_MEMBER': 'broker-CH',
      'RIDE_SUMMONED': '1',
      'RIDE_MAY_SUMMON': 'reviewer',
      'RIDE_PERMITS': 'bro.party.join',
      'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
    }
    assert 'BRO_STORE' not in member.env  # delivered into the container at spawn
    shutil.rmtree(records)

  def test_boxed_join_requires_a_running_container(self, lowering_harness, monkeypatch, tmp_path):
    workspace = Workspace.ensure(PARENT, None, Isolation.BOXED)
    monkeypatch.setattr(ride.session, 'find_container_id', lambda tree: None)
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace.tree,
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      party='join',
      isolation=None,
    )
    runtime_bundle = MagicMock(spec=RuntimeBundle)
    runtime_bundle.host_root = Path('/runtime')
    runtime_bundle.recorded_reference = None
    with pytest.raises(RuntimeError, match='no running container'):
      ride.bro_worker._lower_join(launch, 'broker-CH', runtime_bundle, _artifacts())

  def test_boxed_member_records_under_its_own_party_records(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    # a local-trails member joining a service-backed or --no-trails first session
    # cannot use that session's /var/ride/trails bind, which is absent for its
    # scope; it records under the party mount its own records live in, which the
    # container always carries, and it records mandatorily (never --no-trails)
    lowered = self._boxed_join(monkeypatch, tmp_path)
    workspace = Workspace.open(PARENT)
    records = party_member_dir(workspace.path, 'broker-CH')
    assert lowered.launch.env['RIDE_TRAILS_ROOT'] == '/var/ride/party/broker-CH/trails'
    assert (records / 'trails').is_dir()
    assert '--no-trails' not in lowered.launch.command  # the member records mandatorily
    shutil.rmtree(records)

  @pytest.mark.asyncio
  async def test_unboxed_started_child_keeps_failed_workspace_without_credentials(
    self, lowering_harness, tmp_path
  ):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='detached-root',
      parent_tree=workspace_tree('detached-root'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      isolation=Isolation.UNBOXED,
    )
    lowered = ride.bro_worker._lower_summon(
      launch,
      'broker-CH',
      _runtime_bundle(tmp_path),
      _container_runtime(),
      _artifacts(),
    )
    assert isinstance(lowered, workspace_spawn.ProcessLaunchSpec)
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
    channel = Provisioned(
      channel='CH',
      host_endpoint=Endpoint(port=7321, token='tk'),
    )

    handle = await workspace_spawn.ProcessSpawner().spawn(
      lowered, channel, 'X-1', frozenset({'worker.say'})
    )

    assert await handle.wait() == 3
    assert 'aws-hook-installed' in handle.output_tail()
    assert not private_store.exists()
    workspace = Workspace.open('broker-CH')
    assert workspace.isolation is Isolation.UNBOXED
    assert not (workspace.path / 'environment').exists()

  def test_unboxed_lowering_surfaces_private_store_cleanup_failure(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='detached-root',
      parent_tree=workspace_tree('detached-root'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      isolation=Isolation.UNBOXED,
    )

    def fail_launch(*_args, **_kwargs):
      raise ValueError('launch failed')

    monkeypatch.setattr(ride.session, 'started_party_launch', fail_launch)

    def fail_cleanup(path):
      del path
      raise OSError('cleanup refused')

    monkeypatch.setattr(ride.bro_worker.shutil, 'rmtree', fail_cleanup)

    with pytest.raises(OSError, match='cleanup refused'):
      ride.bro_worker._lower_summon(
        launch,
        'broker-CH',
        _runtime_bundle(tmp_path),
        _container_runtime(),
        _artifacts(),
      )

  def test_unreadable_parent_head_fails_the_spawn(self, lowering_harness):
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent='gone',
      parent_tree=workspace_tree('gone'),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    with pytest.raises(ValueError, match="summoner's HEAD"):
      _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())

  @pytest.mark.asyncio
  async def test_spawner_lowers_off_loop_and_delegates_to_docker(self, lowering_harness):
    class RecordingDocker(workspace_spawn.DockerSpawner):
      def __init__(self):
        self.spawned: list = []

      async def spawn(self, launch, channel, mission, talk):
        self.spawned.append((launch, channel, mission, talk))
        return MagicMock()

    docker = RecordingDocker()
    facts = _facts_expecting('X-1')
    runtime_bundle = MagicMock(spec=RuntimeBundle)
    runtime_bundle.recorded_reference = None
    runtime_bundle.host_root = Path('/runtime')
    spawner = ride.bro_worker.SummonSpawner(
      docker,
      workspace_spawn.ProcessSpawner(),
      workspace_spawn.ExecSpawner(),
      runtime_bundle,
      _container_runtime(),
      facts,
      _artifacts(),
    )
    channel = Provisioned(channel='CH', host_endpoint=Endpoint(port=7321, token='tk'))
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
    )
    await spawner.spawn(launch, channel, 'X-1', frozenset({'worker.say'}))
    [(lowered, lowered_channel, lowered_quest, lowered_talk)] = docker.spawned
    assert isinstance(lowered, workspace_spawn.DockerLaunchSpec)
    assert lowered.launch.command == [
      'do-ride', 'solo', '--workspace', 'broker-CH', '--harness', 'bro', '--repo', '/proj',
      '--hold', 'unattended', 'dev', 'p',
    ]  # fmt: skip
    assert lowered.launch.name == 'broker-CH'
    assert lowered_channel is channel
    assert lowered_quest == 'X-1'
    assert lowered_talk == frozenset({'worker.say'})

  @pytest.mark.asyncio
  async def test_boxed_join_records_an_artifact_view(self, lowering_harness, monkeypatch):
    class RecordingExec(workspace_spawn.ExecSpawner):
      def __init__(self):
        pass

      async def spawn(self, launch, channel, mission, talk):
        return MagicMock()

    parent = Workspace.ensure('nested-parent', None, Isolation.BOXED)
    facts = _facts_expecting('X-1')
    lowered = workspace_spawn.ExecLaunchSpec(
      launch=MagicMock(),
      party_workspace=parent.name,
      records_directory=str(parent.path / 'party' / 'member'),
    )
    monkeypatch.setattr(ride.bro_worker, '_lower_join', lambda *args: lowered)
    spawner = ride.bro_worker.SummonSpawner(
      MagicMock(),
      MagicMock(),
      RecordingExec(),
      MagicMock(),
      _container_runtime(),
      facts,
      _artifacts(),
    )
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=parent.name,
      parent_tree=parent.tree,
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      party='join',
      isolation=None,
    )

    await spawner.spawn(
      launch,
      Provisioned(channel='CH', host_endpoint=Endpoint(port=7321, token='tk')),
      'X-1',
      frozenset(),
    )

    assert facts.for_mission('X-1').artifact_view

  @pytest.mark.asyncio
  async def test_lowering_failure_propagates_out_of_spawn(self, lowering_harness):
    spawner = ride.bro_worker.SummonSpawner(
      workspace_spawn.DockerSpawner(),
      workspace_spawn.ProcessSpawner(),
      workspace_spawn.ExecSpawner(),
      MagicMock(),
      _container_runtime(),
      _facts_expecting('X-1'),
      _artifacts(),
    )
    channel = Provisioned(channel='CH', host_endpoint=Endpoint(port=7321, token='tk'))
    launch = ride.bro_worker.SummonLaunchSpec(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      may_summon=(),
      harness='bro',
      into='nope',
    )
    # the raise crosses to_thread back onto the loop: Dispatcher.spawn turns it
    # into the correlated failed{reason: 'launch'}
    with pytest.raises(ValueError, match='nope'):
      await spawner.spawn(launch, channel, 'X-1', frozenset({'worker.say'}))


def test_unboxed_root_can_chain_joins_and_end_a_started_childs_party_without_a_daemon(
  lowering_harness, tmp_path
):
  workspace = Workspace.ensure('party-root', None, Isolation.UNBOXED)
  workspace.tree.mkdir(parents=True)
  report = tmp_path / 'answer.txt'
  runtime_bundle = _runtime_bundle(tmp_path)
  member_executable = runtime_bundle.host_venv / 'bin' / 'do-ride'
  member_source = f"""#!{sys.executable}
import os
import signal
import sys
import time
from pathlib import Path

from bro.launch.broxy import session_broxy
from bro.run_lifecycle import RunLifecycle
from bro.summon import summon_and_wait, summon_detached

prompt = sys.argv[-1]
if prompt in ('first', 'second'):
  assert Path.cwd() == Path({str(workspace.tree)!r})
  assert os.environ['RIDE_PARTY_MEMBER'].startswith('broker-')
elif prompt == 'owner':
  assert Path.cwd() != Path({str(workspace.tree)!r})
  assert 'RIDE_PARTY_MEMBER' not in os.environ
else:
  assert Path.cwd() != Path({str(workspace.tree)!r})
  assert os.environ['RIDE_PARTY_MEMBER'].startswith('broker-')
with session_broxy():
  lifecycle = RunLifecycle.from_env()
  assert lifecycle is not None
  lifecycle.trail('trail-' + prompt)
  if prompt == 'first':
    answer = summon_and_wait('dev', 'second', party='join', timeout=20)
    lifecycle.completed('first:' + answer, 'ok')
  elif prompt == 'second':
    lifecycle.completed('second', 'ok')
  elif prompt == 'owner':
    summon_detached('dev', 'linger', party='join', timeout=20)
    marker = Path('linger-started')
    deadline = time.monotonic() + 10
    while not marker.exists():
      assert time.monotonic() < deadline
      time.sleep(0.05)
    lifecycle.completed('owner', 'ok')
  else:
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    Path('linger-started').touch()
    time.sleep(30)
  lifecycle.close()
"""
  member_executable.write_text(member_source)
  member_executable.chmod(0o755)
  root_source = f"""
import time
from pathlib import Path
from bro.launch.broxy import session_broxy
from bro.summon import summon_and_wait

with session_broxy():
  answer = summon_and_wait(
    'dev',
    'first',
    party='join',
    grant=['@dev', ':bro.party.join'],
    timeout=20,
  )
  owner = summon_and_wait(
    'dev',
    'owner',
    isolation='unboxed',
    grant=['@dev', ':bro.party.join'],
    timeout=20,
  )
  Path({str(report)!r}).write_text(answer + '|' + owner)
  time.sleep(1)
"""
  launch = workspace_spawn.ProcessLaunchSpec(
    command=[sys.executable, '-c', root_source],
    cwd=str(workspace.tree),
    env=dict(os.environ),
    interactive=False,
  )

  code = ride.broker_root.run_root_via_broker(
    launch,
    workspace=workspace,
    bro='bro-dev',
    may_summon={'dev'},
    permits={'bro.party.join', 'bro.party.start.unboxed'},
    credential_scope=workspace_store.ScopedSecrets(set(), set()),
    container_runtime=_container_runtime(),
    runtime_bundle=runtime_bundle,
  )

  assert code == 0
  assert report.read_text() == 'first:second|owner'
  party = workspace.path / 'party'
  assert not party.exists() or list(party.iterdir()) == []
  workspaces = [
    path.name for path in workspace.path.parent.iterdir() if (path / 'workspace.json').is_file()
  ]
  assert workspaces == ['party-root']


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

  def _launch(self, **overrides) -> ride.bro_worker.SummonLaunchSpec:
    fields: dict = {
      'target': 'dev',
      'prompt': 'deploy the thing',
      'parent': PARENT,
      'parent_tree': workspace_tree(PARENT),
      'repo': Path('/proj'),
      'summoner': SUMMONER,
      'may_summon': (),
      'harness': 'claude',
      **overrides,
    }
    return ride.bro_worker.SummonLaunchSpec(**fields)

  def test_unboxed_join_provisions_claude_state_under_the_member_records(
    self, claude_harness, monkeypatch, tmp_path
  ):
    from bro.monitor import CLAUDE_CONFIG_DIR_ENV
    from ride.claude.harness import CLAUDE

    workspace = Workspace.ensure(PARENT, None, Isolation.UNBOXED)
    workspace.tree.mkdir(parents=True)
    seen: list[tuple[Path, Path]] = []

    def prepare(spec, records, tree, env):
      del spec
      seen.append((records, tree))
      claude = records / 'claude'
      claude.mkdir()
      env[CLAUDE_CONFIG_DIR_ENV] = str(claude)

    monkeypatch.setattr(CLAUDE, 'prepare_unboxed_env', prepare)
    lowered = ride.bro_worker._lower_join(
      self._launch(
        repo=None,
        parent_tree=workspace.tree,
        party='join',
        isolation=None,
      ),
      'broker-CH',
      _runtime_bundle(tmp_path),
      _artifacts(),
    )
    assert isinstance(lowered, workspace_spawn.ProcessLaunchSpec)
    records = party_member_dir(workspace.path, 'broker-CH')
    assert seen == [(records, workspace.tree)]
    assert lowered.env[CLAUDE_CONFIG_DIR_ENV] == str(records / 'claude')
    assert lowered.cleanup_directory is not None
    shutil.rmtree(lowered.cleanup_directory)
    shutil.rmtree(records)

  def test_boxed_join_provisions_claude_state_under_the_member_records(
    self, claude_harness, monkeypatch, tmp_path
  ):
    import json

    from bro.monitor import CLAUDE_CONFIG_DIR_ENV

    workspace = Workspace.ensure(PARENT, None, Isolation.BOXED)
    monkeypatch.setattr(ride.session, 'find_container_id', lambda tree: 'cid-party')
    runtime_bundle = MagicMock(spec=RuntimeBundle)
    runtime_bundle.host_root = Path('/runtime')
    runtime_bundle.recorded_reference = None

    lowered = ride.bro_worker._lower_join(
      self._launch(repo=None, parent_tree=workspace.tree, party='join', isolation=None),
      'broker-CH',
      runtime_bundle,
      _artifacts(),
    )

    assert isinstance(lowered, workspace_spawn.ExecLaunchSpec)
    records = party_member_dir(workspace.path, 'broker-CH')
    assert lowered.launch.env[CLAUDE_CONFIG_DIR_ENV] == '/var/ride/party/broker-CH/claude'
    assert lowered.launch.env['DISABLE_INSTALLATION_CHECKS'] == '1'
    settings = json.loads((records / 'claude' / 'settings.json').read_text())
    assert settings['skipDangerousModePermissionPrompt'] is True
    seeded = json.loads((records / 'claude' / '.claude.json').read_text())
    assert seeded['projects'] == {'/workspace': {'hasTrustDialogAccepted': True}}
    shutil.rmtree(records)

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
      'RIDE_PERMITS': 'bro.party.start.boxed',
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
    }
    assert lowered.launch.base_ref == 'PARENT-SHA'
    assert lowered.launch.extra_mounts == (
      '/host/claude:/home/ride/.claude',
      _session_state_mount('broker-CH'),
      _party_mount('broker-CH'),
      ride.artifacts.view_mount(SESSION, 'broker-CH'),
    )
    assert lowered.launch.tty is False

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

  def test_the_partys_env_additions_reach_a_started_child(self, claude_harness, tmp_path):
    lowered = _lower_boxed(
      self._launch(env={'IS_SANDBOX': '1'}), 'broker-CH', _container_runtime(), _artifacts()
    )
    assert lowered.launch.additions == {'IS_SANDBOX': '1'}
    spec = ride.session.load_resume_spec(Workspace.open('broker-CH'))
    assert spec is not None
    assert spec.env == {'IS_SANDBOX': '1'}

  def test_scope_follows_the_claude_recipe(self, claude_harness, monkeypatch):
    captured: list = []

    def capture_scope(
      name,
      recipe,
      attachment=None,
      attachment_repository=None,
      llm_spec=None,
      grant=(),
      revoke=(),
      check_selection=True,
    ):
      captured.append(recipe.name)
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.bro_worker, 'scoped_secrets', capture_scope)
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
    monkeypatch.setattr(ride.broker_root, 'bridge_gateway', lambda: None)
    assert ride.broker_root.broker_bind_hosts() == [LOCAL_HOST]

  def test_a_gateway_that_is_no_address_here_binds_loopback_alone(self, monkeypatch):
    # what a daemon in a VM reports: its own gateway, which this host cannot bind
    monkeypatch.setattr(ride.broker_root, 'bridge_gateway', lambda: '192.0.2.1')
    assert ride.broker_root.broker_bind_hosts() == [LOCAL_HOST]


class TestRunRootViaBroker:
  def test_wires_registered_types_through_launch(self, monkeypatch, tmp_path):
    from bro.worker_types import LaunchRequest, WorkerType

    captured: dict = {}

    class TestWorkerType(WorkerType):
      name = 'test'

      def talk(self, request: LaunchRequest):
        return frozenset()

      def launch(self, request: LaunchRequest):
        raise NotImplementedError

    class FakeBroker:
      def __init__(self, transport, **kwargs):
        captured['handlers'] = {}
        captured['observers'] = []
        self.journal = Journal()

      def on(self, kind, handler):
        captured['handlers'][kind] = handler

      def subscribe(self, observer):
        captured['observers'].append(observer)

      def run(self, launch, spawner, *, type, end_on_sigterm=False):
        captured.update(launch=launch, spawner=spawner, type=type, end=end_on_sigterm)
        return 3

    monkeypatch.setattr(ride.broker_root, 'Broker', FakeBroker)
    monkeypatch.setattr(ride.broker_root, 'broker_bind_hosts', lambda: [LOCAL_HOST])
    launch = workspace_spawn.ProcessLaunchSpec(command=['x'], cwd='/', env={})
    workspace = Workspace.create('ws', tmp_path / 'proj', Isolation.BOXED)

    assert (
      ride.broker_root.run_root_via_broker(
        launch,
        workspace=workspace,
        bro='bro-dev',
        credential_scope=workspace_store.ScopedSecrets({'harbor'}, set()),
        container_runtime=_container_runtime(),
        runtime_bundle=MagicMock(),
        types={'test': TestWorkerType},
      )
      == 3
    )
    assert set(captured['handlers']) == {
      'ping',
      'launch',
      'artifact.mint',
      'artifact.get',
    }
    assert isinstance(captured['spawner'], workspace_spawn.ProcessSpawner)
    assert captured['type'] == 'bro'
    assert captured['end'] is True
    assert captured['launch'] is launch
