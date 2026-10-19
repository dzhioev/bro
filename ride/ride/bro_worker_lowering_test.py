import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import cast
from unittest.mock import MagicMock

import pytest

import bro.native.harness as native_harness
import ride.artifacts
import ride.authority_launch
import ride.bro_worker
import ride.broker_root
import ride.do_ride
import ride.harness
import ride.identity
import ride.peer_facts
import ride.session
import ride.workspace.docker as workspace_docker
import ride.workspace.spawn as workspace_spawn
import ride.workspace.store as workspace_store
from bro.authority.records import dump_request
from bro.authority.rules import Mission
from bro.base import credentials
from bro.base.host_config import Attachment
from bro.bro import BaseBro
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
  CONTAINER_ARTIFACTS_ROOT,
  CONTAINER_PARTY_DIR,
  CONTAINER_SESSION_DIR,
  workspace_dir,
  workspace_tree,
)
from ride.authority_launch import credentials_for
from ride.authority_reading import Reading
from ride.authority_scope import checked_recipe
from ride.authority_test_helper import mission, reading
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


def _do_ride_environment(
  workspace_name: str, record: Mission, base_sha: str = 'PARENT-SHA'
) -> dict[str, str]:
  assert isinstance(record.reading, Reading)
  workspace = Workspace.open(workspace_name)
  spec = ride.session.load_resume_spec(workspace)
  assert spec is not None
  assert workspace.metadata.branch is not None
  return {
    ride.do_ride.INSTALL_DIRECTORY_ENV: ride.do_ride.CONTAINER_INSTALL_DIRECTORY,
    ride.do_ride.RESOLVED_LLM_ENV: ride.do_ride.encode_resolved_llm(
      checked_recipe(record.request, record.descriptor, record.reading).dump()
    ),
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


def _launch(**fields) -> ride.bro_worker.SummonLaunchSpec:
  source = fields.pop('source', None)
  values = {'harness': fields['harness']}
  for name in ('llm', 'hold'):
    if fields.get(name) is not None:
      values[name] = fields[name]
  values['placement'] = (
    'join' if fields.get('party') == 'join' else fields.get('isolation', Isolation.BOXED).value
  )
  if fields.get('passes'):
    values['pass'] = fields['passes']
  record = mission(
    target=fields['target'],
    source=source,
    values=values,
    grant=fields.get('grant', ()),
    revoke=fields.get('revoke', ()),
  )
  owned = {
    name: value
    for name, value in fields.items()
    if name in {'prompt', 'parent', 'parent_tree', 'summoner', 'repo', 'into', 'share', 'env'}
  }
  return ride.bro_worker.SummonLaunchSpec(record=record, **owned)


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
      extension=ride.bro_worker.BroFacts('bro-dev'),
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
      extension=ride.bro_worker.BroFacts('dev'),
    ),
  )
  return facts


@pytest.fixture
def lowering_harness(monkeypatch, tmp_path, register_test_bros):
  class Dev(BaseBro):
    name = 'dev'

  register_test_bros(Dev)

  def credential_scope(record):
    selection = {
      name.removeprefix('cred.'): value
      for name, value in record.descriptor.values.items()
      if name.startswith('cred.')
    }
    return workspace_store.ScopedSecrets(
      {'aws', 'trails'} | (selection.keys() - {'openai'}), {'openai'}, selection
    )

  monkeypatch.setattr(ride.authority_launch, 'credentials_for', credential_scope)
  monkeypatch.setattr(
    ride.authority_launch,
    'hydrate',
    lambda scoped: ({}, frozenset(scoped.required | scoped.optional)),
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
    launch = _launch(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
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
          '--',
          'dev',
          'deploy the thing',
        ],  # fmt: skip
        env={
          **_do_ride_environment('broker-CH', launch.record),
          'RIDE_BRO': 'dev',
          'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness bro dev deploy the thing',
          'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
          'RIDE_SUMMONED': '1',
          'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
        },
        secrets={'aws', 'trails'},
        optional_secrets={'openai'},
        credential_selection={
          name.removeprefix('cred.'): cast(str, value)
          for name, value in launch.record.descriptor.values.items()
          if name.startswith('cred.')
        },
        credential_store={},
        hydrated_kinds=frozenset({'aws', 'openai', 'trails'}),
        tty=False,
        image='runtime-image',
        runtime_bundle_hash='bundle-hash',
        extra_mounts=(
          _session_state_mount('broker-CH'),
          _party_mount('broker-CH'),
          ride.artifacts.view_mount(SESSION, 'broker-CH', PurePosixPath(CONTAINER_ARTIFACTS_ROOT)),
        ),
        repo=Path('/proj'),
        base_ref='PARENT-SHA',
      ),
    )

  def test_lowering_logs_the_scope_like_any_container_launch(self, lowering_harness, caplog):
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
    )
    with caplog.at_level('INFO'):
      _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert 'scoped secrets for summoned dev: aws, trails' in caplog.text

  def test_hold_rides_the_childs_do_ride_argv(self, lowering_harness):
    launch = _launch(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
      hold='attended',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command[-5:] == ['--hold', 'attended', '--', 'dev', 'deploy the thing']

  def test_the_llm_recipe_reaches_the_childs_resolved_environment(self, lowering_harness):
    launch = _launch(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
      llm='openai:sol:high+fast',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert (
      json.loads(lowered.launch.env[ride.do_ride.RESOLVED_LLM_ENV])
      == checked_recipe(
        launch.record.request, launch.record.descriptor, cast(Reading, launch.record.reading)
      ).dump()
    )

  def test_the_hosts_per_bro_recipe_settles_the_childs_llm(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    source = replace(
      reading(config={'projects': {'/proj': {'bros': {'dev': {'llm': ['openai:sol:xhigh']}}}}}),
      attachment=Attachment(path='/proj'),
    )
    monkeypatch.setattr('bro.base.host_config._read', lambda: pytest.fail('launch reread policy'))
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
      llm='::low',
      source=source,
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    resolved = json.loads(lowered.launch.env[ride.do_ride.RESOLVED_LLM_ENV])
    assert resolved == native_harness.BRO.resolve_llm('openai:sol:low', 'dev').dump()

  def test_the_llm_recipe_selects_the_childs_hydrated_llm_key(self, lowering_harness, monkeypatch):
    captured: list = []

    def capture_scope(record):
      captured.append(record)
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.authority_launch, 'credentials_for', capture_scope)
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
      llm='echo',
    )
    _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert captured == [launch.record]
    assert launch.record.descriptor.values['provider'] == 'echo'

  def test_lowering_records_the_childs_resume_spec(self, lowering_harness, tmp_path):
    launch = _launch(
      target='dev',
      prompt='deploy the thing',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
      hold='guided',
      llm='openai:sol:high',
      grant=('@reviewer',),
      passes=('github+passed',),
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    workspace = Workspace.open('broker-CH')
    assert workspace.metadata.throwaway
    assert lowered.launch.secrets == {'aws', 'github', 'trails'}
    assert lowered.launch.credential_selection['github'] == 'passed'
    spec = ride.session.load_resume_spec(workspace)
    assert spec is not None
    assert spec.authority_request == dump_request(launch.record.request, launch.record.descriptor)
    assert spec.bro == launch.target
    assert spec.subject == launch.prompt
    assert spec.prompt is None
    assert spec.resume
    assert spec.repo == '/proj'
    assert not spec.drop
    assert not spec.solo

  def test_child_resume_record_carries_a_given_runtime(self, lowering_harness):
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      summoner=SUMMONER,
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
      native_harness.BRO,
      'container_extras',
      lambda spec, workspace, scoped: ride.harness.ContainerExtras(
        env={}, mounts=('/host/state:/state',)
      ),
    )
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.extra_mounts == (
      '/host/state:/state',
      '/host/trails:/var/ride/trails',
      _session_state_mount('broker-CH'),
      _party_mount('broker-CH'),
      ride.artifacts.view_mount(SESSION, 'broker-CH', PurePosixPath(CONTAINER_ARTIFACTS_ROOT)),
    )

  def test_the_child_keeps_session_state_like_any_boxed_session(self, lowering_harness):
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert workspace_session_dir(workspace_dir('broker-CH')).is_dir()
    assert lowered.launch.env[SESSION_DIR_ENV] == str(CONTAINER_SESSION_DIR)
    assert _session_state_mount('broker-CH') in lowered.launch.extra_mounts

  def test_child_authority_is_preserved_in_its_resume_request(self, lowering_harness):
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
      grant=('@bro', '@reviewer'),
    )
    _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    spec = ride.session.load_resume_spec(Workspace.open('broker-CH'))
    assert spec is not None
    assert spec.authority_request == dump_request(launch.record.request, launch.record.descriptor)

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
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=repository,
      summoner=SUMMONER,
      harness='bro',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.env[HUMAN_NAME_ENV] == 'Ada Lovelace'
    assert lowered.launch.env[HUMAN_EMAIL_ENV] == 'ada@example.com'

  def test_into_overrides_the_inherited_base_ref(self, lowering_harness):
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
      into='summon',
    )
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts()).launch
    assert lowered.env == {
      **_do_ride_environment('broker-CH', launch.record, base_sha='REF-SHA'),
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness bro --into summon dev p',
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
    }
    assert lowered.base_ref == 'REF-SHA'

  def test_unresolvable_into_fails_the_spawn(self, lowering_harness):
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
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
    launch = _launch(
      target='dev',
      prompt='p',
      parent='empty',
      parent_tree=workspace_tree('empty'),
      summoner=SUMMONER,
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
    launch = _launch(
      target='dev',
      prompt='work beside me',
      parent=PARENT,
      parent_tree=workspace.tree,
      summoner=SUMMONER,
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
    assert lowered.env['RIDE_SUMMONED'] == '1'
    assert lowered.env['IS_SANDBOX'] == '1'
    assert lowered.cleanup_directory is not None
    shutil.rmtree(lowered.cleanup_directory)
    shutil.rmtree(records)

  def _boxed_join(
    self,
    monkeypatch,
    tmp_path,
    env: dict[str, str] | None = None,
    passes: tuple[str, ...] = (),
  ) -> workspace_spawn.ExecLaunchSpec:
    workspace = Workspace.ensure(PARENT, None, Isolation.BOXED)
    monkeypatch.setattr(ride.session, 'find_container_id', lambda tree: 'cid-party')
    launch = _launch(
      target='dev',
      prompt='work beside me',
      parent=PARENT,
      parent_tree=workspace.tree,
      summoner=SUMMONER,
      harness='bro',
      passes=passes,
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
      '--hold', 'unattended', '--', 'dev', 'work beside me',
    ]  # fmt: skip
    assert member.secrets == {'aws', 'trails'}
    assert member.optional_secrets == {'openai'}
    ride_command = shlex.join(
      [
        'summon',
        '--join',
        '--hold',
        member.command[member.command.index('--hold') + 1],
        '--harness',
        'bro',
        'dev',
        'work beside me',
      ]
    )
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
      'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
    }
    assert 'BRO_STORE' not in member.env  # delivered into the container at spawn
    shutil.rmtree(records)

  def test_joined_child_launch_carries_the_pass_scope(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    workspace = Workspace.ensure(PARENT, None, Isolation.BOXED)
    lowered = self._boxed_join(
      monkeypatch,
      tmp_path,
      passes=('github+passed',),
    )

    assert lowered.launch.secrets == {'aws', 'github', 'trails'}
    assert lowered.launch.credential_selection['github'] == 'passed'
    arguments = shlex.split(lowered.launch.env['RIDE_COMMAND'])
    assert arguments[:2] == ['summon', '--join']
    assert arguments[arguments.index('--pass') + 1] == 'github+passed'
    shutil.rmtree(party_member_dir(workspace.path, 'broker-CH'))

  def test_boxed_join_requires_a_running_container(self, lowering_harness, monkeypatch, tmp_path):
    workspace = Workspace.ensure(PARENT, None, Isolation.BOXED)
    monkeypatch.setattr(ride.session, 'find_container_id', lambda tree: None)
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace.tree,
      summoner=SUMMONER,
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
    launch = _launch(
      target='dev',
      prompt='p',
      parent='detached-root',
      parent_tree=workspace_tree('detached-root'),
      summoner=SUMMONER,
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
    launch = _launch(
      target='dev',
      prompt='p',
      parent='detached-root',
      parent_tree=workspace_tree('detached-root'),
      summoner=SUMMONER,
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
    launch = _launch(
      target='dev',
      prompt='p',
      parent='gone',
      parent_tree=workspace_tree('gone'),
      repo=Path('/proj'),
      summoner=SUMMONER,
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
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
      harness='bro',
    )
    await spawner.spawn(launch, channel, 'X-1', frozenset({'worker.say'}))
    [(lowered, lowered_channel, lowered_quest, lowered_talk)] = docker.spawned
    assert isinstance(lowered, workspace_spawn.DockerLaunchSpec)
    assert lowered.launch.command == [
      'do-ride', 'solo', '--workspace', 'broker-CH', '--harness', 'bro', '--repo', '/proj',
      '--hold', 'unattended', '--', 'dev', 'p',
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
    launch = _launch(
      target='dev',
      prompt='p',
      parent=parent.name,
      parent_tree=parent.tree,
      summoner=SUMMONER,
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

    assert facts.for_mission('X-1').artifact_view == PurePosixPath(CONTAINER_ARTIFACTS_ROOT)

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
    launch = _launch(
      target='dev',
      prompt='p',
      parent=PARENT,
      parent_tree=workspace_tree(PARENT),
      repo=Path('/proj'),
      summoner=SUMMONER,
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
    answer = summon_and_wait('dev', 'second', placement='join', timeout=20)
    lifecycle.completed('first:' + answer, 'ok')
  elif prompt == 'second':
    lifecycle.completed('second', 'ok')
  elif prompt == 'owner':
    summon_detached('dev', 'linger', placement='join', timeout=20)
    marker = Path('linger-started')
    deadline = time.monotonic() + 10
    while not marker.exists():
      assert time.monotonic() < deadline
      time.sleep(0.05)
    lifecycle.completed('owner', 'ok')
  else:
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    Path('linger-started').touch()
    signal.pause()
  lifecycle.close()
"""
  member_executable.write_text(member_source)
  member_executable.chmod(0o755)
  root_source = f"""
from pathlib import Path
from bro.broker.client import Client
from bro.launch.broxy import session_broxy
from bro.mission import query_mission
from bro.summon import summon_detached

with session_broxy():
  client = Client.from_env()
  assert client is not None
  with client:
    def completed(prompt, placement):
      mission = summon_detached('dev', prompt, placement=placement,
        grant=['@dev', 'bro.placement=join'], timeout=20)
      record = query_mission(client, mission, wait_seconds=20, wait_for_settlement=True)
      assert record['settled'], record
      assert record['result']['outcome'] == 'ok', record
      return record['result']['value']
    answer = completed('first', 'join')
    owner = completed('owner', 'unboxed')
    Path({str(report)!r}).write_text(answer + '|' + owner)
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
    mission=mission(grant=('@dev', 'bro.placement=join', 'bro.placement=unboxed')),
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

    monkeypatch.setattr(CLAUDE, 'preflight_auth', lambda spec, store: None)
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
      'harness': 'claude',
      **overrides,
    }
    return _launch(**fields)

  def test_unboxed_join_provisions_claude_state_under_the_member_records(
    self, claude_harness, monkeypatch, tmp_path
  ):
    from ride.claude.claude_config import CLAUDE_CONFIG_DIR_ENV
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

    from ride.claude.claude_config import CLAUDE_CONFIG_DIR_ENV

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
    launch = self._launch()
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert lowered.launch.command == [
      'do-ride', 'solo', '--workspace', 'broker-CH', '--harness', 'claude', '--repo', '/proj',
      '--hold', 'unattended', '--', 'dev', 'deploy the thing',
    ]  # fmt: skip
    assert lowered.launch.env == {
      'CLAUDE_CONFIG_DIR': '/home/ride/.claude',
      **_do_ride_environment('broker-CH', launch.record),
      'RIDE_BRO': 'dev',
      'RIDE_COMMAND': 'ride solo --repo /proj --hold unattended --harness claude dev deploy the thing',
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
      'RIDE_SUMMONED': '1',
      'RIDE_SUMMONER': '{"trail_id":"T-parent"}',
    }
    assert lowered.launch.base_ref == 'PARENT-SHA'
    assert lowered.launch.extra_mounts == (
      '/host/claude:/home/ride/.claude',
      _session_state_mount('broker-CH'),
      _party_mount('broker-CH'),
      ride.artifacts.view_mount(SESSION, 'broker-CH', PurePosixPath(CONTAINER_ARTIFACTS_ROOT)),
    )
    assert lowered.launch.tty is False

  def test_records_the_claude_resume_spec(self, claude_harness, tmp_path):
    launch = self._launch(llm=':fable5', summon_depth=5, summon_harness='claude')
    _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    spec = ride.session.load_resume_spec(Workspace.open('broker-CH'))
    assert spec is not None
    assert spec.harness == 'claude'
    assert spec.authority_request == dump_request(launch.record.request, launch.record.descriptor)

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

    def capture_scope(record):
      captured.append(record.descriptor.values['harness'])
      return workspace_store.ScopedSecrets(required=set(), optional=set())

    monkeypatch.setattr(ride.authority_launch, 'credentials_for', capture_scope)
    _lower_boxed(self._launch(), 'broker-CH', _container_runtime(), _artifacts())
    assert captured == ['claude']

  def test_missing_harness_auth_fails_the_spawn_before_the_workspace(
    self, lowering_harness, monkeypatch, tmp_path
  ):
    monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path / 'empty-store'))
    monkeypatch.setattr(ride.authority_launch, 'credentials_for', credentials_for)
    with pytest.raises(ValueError, match=r'missing.*claude_code'):
      _lower_boxed(self._launch(), 'broker-CH', _container_runtime(), _artifacts())
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  def test_a_native_recipe_fails_the_claude_spawn(self, claude_harness, tmp_path):
    with pytest.raises(ValueError, match="the claude harness cannot run recipe 'openai:sol'"):
      _lower_boxed(self._launch(llm='openai:sol'), 'broker-CH', _container_runtime(), _artifacts())
    with pytest.raises(ValueError, match='broker-CH'):
      Workspace.open('broker-CH')

  def test_a_claude_recipe_reaches_the_resolved_environment(self, claude_harness):
    launch = self._launch(llm=':fable5:high')
    lowered = _lower_boxed(launch, 'broker-CH', _container_runtime(), _artifacts())
    assert (
      json.loads(lowered.launch.env[ride.do_ride.RESOLVED_LLM_ENV])
      == checked_recipe(
        launch.record.request, launch.record.descriptor, cast(Reading, launch.record.reading)
      ).dump()
    )

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
    from ride.operator import MISSIONS

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

      def run(self, launch, spawner, *, type, timeout=None, end_on_sigterm=False, lifetime=None):
        captured.update(
          launch=launch, spawner=spawner, type=type, end=end_on_sigterm, lifetime=lifetime
        )
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
        mission=mission(),
        container_runtime=_container_runtime(),
        runtime_bundle=MagicMock(),
        types={'test': TestWorkerType},
      )
      == 3
    )
    assert set(captured['handlers']) == {
      MISSIONS,
      'ping',
      'launch',
      'artifact.mint',
      'artifact.get',
      'artifact.share',
      'authority',
      'operator.commit',
      'operator.claim',
      'operator.override',
    }
    assert isinstance(captured['spawner'], workspace_spawn.ProcessSpawner)
    assert captured['type'] == 'bro'
    assert captured['end'] is True
    assert callable(captured['lifetime'])
    assert captured['launch'] is launch
