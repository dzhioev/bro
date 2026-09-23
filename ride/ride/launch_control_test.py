import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bro.broker.brotocol import Message, Talk, request
from bro.broker.job import CommandJob
from bro.broker.spawn import Spawner
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.worker_types import (
  ArtifactDenied,
  Container,
  Expect,
  Job,
  LaunchDenied,
  LaunchRequest,
  PeerDescription,
  Spawn,
  UnattributablePeer,
  WorkerContainer,
  WorkerType,
)
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT
from ride import pending_launch
from ride.launch_control import LaunchControl

ROOT = 'ROOT'


class Artifacts:
  def __init__(self):
    self.denied = False
    self.resolved = []

  def resolve(self, ref, peer):
    self.resolved.append((ref, peer))
    if self.denied:
      raise ArtifactDenied('not shared')
    return Path('/artifact')


class Host:
  def __init__(self):
    self.artifacts = Artifacts()


class SampleType(WorkerType):
  name = 'test'
  permits = frozenset({'use'})
  default_timeout = 12.0
  widens_talk = True
  manual = True

  def __init__(self, host, run):
    super().__init__(host)
    self.run = run
    self.calls = []
    self.fields: dict[str, Any] = {
      'detail': {'accepted': True},
      'transition': 'type-owned',
    }

  def talk(self, request: LaunchRequest) -> Talk:
    self.calls.append('talk')
    if request.args.get('deny_talk'):
      raise LaunchDenied('talk refused')
    return cast(Talk, frozenset({'worker.say'}) | request.requested_talk)

  def launch(self, request: LaunchRequest):
    self.calls.append('launch')
    if request.args.get('deny_launch'):
      raise LaunchDenied('run refused')
    return self.run

  def audit_fields(self, extension):
    self.calls.append('audit')
    return self.fields


class FixedTalkType(SampleType):
  widens_talk = False


class Peers:
  def __init__(self, owner):
    self.owner = owner
    self.unattributable = False
    self.facts = {}

  def resolve(self, context, peer):
    if self.unattributable:
      raise UnattributablePeer('no owner')
    return self.owner

  def add(self, mission, facts):
    self.facts[mission] = facts

  def attribution_for_mission(self, journal, mission):
    return {'workspace': self.owner.workspace, 'type': self.owner.type}

  def for_mission(self, mission):
    if mission not in self.facts:
      raise UnattributablePeer('missing facts')
    return self.facts[mission]


class Context:
  def __init__(self):
    self.journal = object()
    self.denied = []
    self.spawned = []
    self.jobs = []
    self.expected = []

  def deny(self, peer, error, *, type=None):
    self.denied.append((peer, error, type))

  def spawn(self, launch, spawner, peer, **kwargs):
    self.spawned.append((launch, spawner, peer, kwargs))

  def job(self, command, peer, **kwargs):
    self.jobs.append((command, peer, kwargs))

  def expect(self, peer, *, type, talk, ready):
    self.expected.append((peer, type, talk))
    ready(Provisioned('channel', Endpoint(7321, 'token')))


@pytest.fixture
def owner(tmp_path):
  return PeerDescription(
    mission='root',
    workspace='ws',
    tree=tmp_path,
    type='bro',
    bro='dev',
    permits=frozenset(),
    member=None,
    expected=False,
    artifact_view=PurePosixPath(CONTAINER_ARTIFACTS_ROOT),
    published_ports=(),
    depth=0,
  )


def _control(tmp_path, owner, run, *, type_class=SampleType, worker_container_spawner=None):
  host = Host()
  worker_type = type_class(host, run)
  peers = Peers(owner)
  runtime = SimpleNamespace(reference='/runtime')
  control = LaunchControl(
    ride='ride',
    types={'test': worker_type},
    peers=peers,
    journal=cast(Any, object()),
    audit_file=tmp_path / 'audit.jsonl',
    runtime_bundle=runtime,
    session_env={'ONE': '1'},
    worker_container_spawner=cast(
      Spawner,
      object() if worker_container_spawner is None else worker_container_spawner,
    ),
  )
  return control, worker_type, peers, host


def _message(**args) -> Message:
  return request('launch', {'type': 'test', **args})


def _handle(control, message):
  context = Context()
  control.handle(cast(Any, context), ROOT, message)
  return context


def test_spawn_resolves_talk_before_launch_and_records_facts(tmp_path, owner):
  run = Spawn(object(), object(), permits=frozenset({'test.use'}))
  control, worker_type, peers, _ = _control(tmp_path, owner, run)
  message = _message(value=1, talk=['owner.say'])
  context = _handle(control, message)

  assert worker_type.calls == ['talk', 'launch', 'audit']
  assert context.spawned[0][3] == {
    'type': 'test',
    'talk': frozenset({'worker.say', 'owner.say'}),
    'timeout': 12.0,
  }
  facts = peers.facts[message.request_id]
  assert facts.type == 'test'
  assert facts.permits == frozenset({'test.use'})
  assert not facts.expected


def test_request_timeout_overrides_the_type_default(tmp_path, owner):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(timeout=3))
  assert context.spawned[0][3]['timeout'] == 3.0


@pytest.mark.parametrize('timeout', [0, -1, True, '3'])
def test_invalid_timeout_is_denied(tmp_path, owner, timeout):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(timeout=timeout))
  assert "'timeout' must be a positive number" in context.denied[0][1]


def test_owner_is_attributed_before_type_lookup(tmp_path, owner):
  control, _, peers, _ = _control(tmp_path, owner, Spawn(object(), object()))
  peers.unattributable = True
  context = _handle(control, request('launch', {'type': 'missing'}))
  assert context.denied[0][1] == 'launch denied: no owner'


def test_unknown_type_lists_installed_types(tmp_path, owner):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, request('launch', {'type': 'missing'}))
  assert "unknown worker type 'missing'; installed types: test" in context.denied[0][1]


@pytest.mark.parametrize(
  'args',
  [
    {'talk': 'worker.say'},
    {'talk': ['worker.say', 'worker.say']},
    {'talk': ['worker.shout']},
    {'share': ['not-a-ref']},
    {'manual': False},
  ],
)
def test_malformed_common_arguments_are_denied(tmp_path, owner, args):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  assert _handle(control, _message(**args)).denied


def test_fixed_talk_type_refuses_a_named_talk_field(tmp_path, owner):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()), type_class=FixedTalkType)
  context = _handle(control, _message(talk=[]))
  assert 'does not accept talk widening' in context.denied[0][1]


def test_share_is_resolved_for_spawn(tmp_path, owner):
  ref = 'sha256:' + 'a' * 64
  control, _, _, host = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(share=[ref]))
  assert context.spawned
  assert host.artifacts.resolved == [(ref, owner)]


def test_unreachable_share_is_denied(tmp_path, owner):
  ref = 'sha256:' + 'a' * 64
  control, _, _, host = _control(tmp_path, owner, Spawn(object(), object()))
  host.artifacts.denied = True
  context = _handle(control, _message(share=[ref]))
  assert 'owner cannot reach' in context.denied[0][1]


def test_container_run_hands_the_spec_and_share_to_the_host_spawner(tmp_path, owner):
  ref = 'sha256:' + 'a' * 64
  spec = WorkerContainer(
    files={'Dockerfile': b'ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n'},
    command=('worker',),
    env={},
    published_ports=(8080,),
  )
  spawner = object()
  control, _, peers, _ = _control(
    tmp_path,
    owner,
    Container(spec, extension={'worker': 'facts'}, permits=frozenset({'test.use'})),
    worker_container_spawner=spawner,
  )
  message = _message(share=[ref])
  context = _handle(control, message)

  [(launch, selected_spawner, peer, options)] = context.spawned
  assert launch.type == 'test'
  assert launch.spec is spec
  assert launch.owner_workspace == owner.workspace
  assert launch.share == (ref,)
  assert selected_spawner is spawner
  assert peer == ROOT
  assert options['type'] == 'test'
  facts = peers.facts[message.request_id]
  assert facts.extension == {'worker': 'facts'}
  assert facts.permits == frozenset({'test.use'})
  assert facts.artifact_view is None


@pytest.mark.parametrize(
  'run',
  [Job(CommandJob(('true',), {})), Expect({})],
)
def test_share_is_denied_for_runs_without_a_host_workspace(tmp_path, owner, run):
  ref = 'sha256:' + 'a' * 64
  control, _, _, _ = _control(tmp_path, owner, run)
  args: dict[str, Any] = {'share': [ref]}
  if isinstance(run, Expect):
    args['manual'] = True
  context = _handle(control, _message(**args))
  assert 'share' in context.denied[0][1]


def test_manual_launch_must_select_expect(tmp_path, owner):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(manual=True))
  assert 'does not support a manual launch' in context.denied[0][1]


def test_expect_requires_manual_launch(tmp_path, owner):
  control, _, _, _ = _control(tmp_path, owner, Expect({}))
  context = _handle(control, _message())
  assert 'requires a manual launch' in context.denied[0][1]


def test_manual_launch_writes_and_discards_the_pending_record(tmp_path, monkeypatch, owner):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  control, _, peers, _ = _control(
    tmp_path,
    owner,
    Expect({'value': 1}),
  )
  message = _message(manual=True)
  _handle(control, message)

  pending = pending_launch.peek(message.request_id)
  assert pending.type == 'test'
  assert pending.extension == {'value': 1}
  assert pending.owner_tree == str(owner.tree)
  assert pending.env == {'ONE': '1'}
  assert peers.facts[message.request_id].expected

  event = SimpleNamespace(mission=message.request_id, transition='ended')
  control.observe_journal(cast(Any, event), cast(Any, SimpleNamespace()))
  with pytest.raises(pending_launch.UnknownToken):
    pending_launch.peek(message.request_id)


def test_audit_fields_are_snapshotted_at_acceptance(tmp_path, owner):
  message = _message()
  control, worker_type, peers, _ = _control(tmp_path, owner, Spawn(object(), object()))
  _handle(control, message)
  worker_type.fields['detail']['accepted'] = False
  event = SimpleNamespace(
    mission=message.request_id,
    transition='accepted',
    view=lambda: {'mission': message.request_id, 'transition': 'accepted'},
  )
  record = SimpleNamespace(parent='root', type='test')
  control.audit_event(cast(Any, event), cast(Any, record))
  [entry] = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
  assert entry['owner'] == {'workspace': 'ws', 'type': 'bro'}
  assert entry['type'] == 'test'
  assert entry['published_ports'] == []
  assert entry['transition'] == 'accepted'
  assert entry['extension'] == {
    'detail': {'accepted': True},
    'transition': 'type-owned',
  }
  assert peers.facts[message.request_id].type == 'test'
