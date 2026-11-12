import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bro.authority.authority import Authority, Cap, Entry, Pair, Permission
from bro.authority.rules import Declaration, Option
from bro.authority.values import IntegerInterval, Integers, Strings, Subsets
from bro.broker.brotocol import TALK_RIGHTS, Message, Talk, request
from bro.broker.job import CommandJob
from bro.broker.spawn import Spawner
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.worker_types import (
  ArtifactDenied,
  Container,
  Expect,
  Extension,
  Job,
  LaunchDenied,
  LaunchRequest,
  OptionValues,
  PeerDescription,
  Spawn,
  UnattributablePeer,
  WorkerContainer,
  WorkerType,
  installed_types,
)
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT
from ride import pending_launch
from ride.authority_test_helper import mission, reading
from ride.launch_control import LaunchControl
from ride.peer_facts import WorkerFacts

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

  @classmethod
  def authority_declaration(cls, credential_kinds):
    return Declaration(
      {
        'timeout': Option('scalar', Integers((IntegerInterval(1, None),)), open=True),
        'talk': Option('subset', Subsets(TALK_RIGHTS).universe(), open=True),
      },
      defaults=lambda values, reading: {'timeout': 12},
    )

  @classmethod
  def option_values(cls, fields, reading):
    values = {}
    if 'timeout' in fields:
      values['timeout'] = fields['timeout']
    if 'talk' in fields:
      values['talk'] = frozenset(fields['talk'])
    unknown = fields.keys() - {
      'talk',
      'timeout',
      'manual',
      'share',
      'value',
      'deny_talk',
      'deny_launch',
    }
    if unknown:
      raise ValueError(f'declaration check: unknown fields {sorted(unknown)}')
    return OptionValues(
      values,
      {
        name: value
        for name, value in fields.items()
        if name in {'value', 'deny_talk', 'deny_launch'}
      },
    )

  default_timeout = 12.0
  widens_talk = True
  manual = True

  def __init__(self, host, run):
    super().__init__(host)
    self.run = run
    self.calls = []
    self.requests = []

  def talk(self, request: LaunchRequest) -> Talk:
    self.calls.append('talk')
    if request.args.get('deny_talk'):
      raise LaunchDenied('talk refused')
    return cast(Talk, frozenset({'worker.say'}) | request.requested_talk)

  def launch(self, request: LaunchRequest):
    self.calls.append('launch')
    self.requests.append(request)
    if request.args.get('deny_launch'):
      raise LaunchDenied('run refused')
    return self.run


@dataclass(frozen=True)
class SampleFacts(Extension):
  detail: Any

  def view(self) -> Mapping[str, Any]:
    return {'detail': self.detail}


class FixedTalkType(SampleType):
  widens_talk = False


class PassType(SampleType):
  @classmethod
  def authority_declaration(cls, credential_kinds):
    base = super().authority_declaration(credential_kinds)
    return replace(
      base, options={**base.options, 'cred.github': Option('scalar', Strings(cofinite=True))}
    )

  @classmethod
  def option_values(cls, fields, reading):
    from bro.base.credentials import parse_picks

    mapped = super().option_values(
      {key: value for key, value in fields.items() if key != 'pass'}, reading
    )
    values = {**mapped.values}
    if 'pass' in fields:
      values.update(
        {
          'cred.' + kind: instance
          for kind, instance in parse_picks(fields['pass'], where='test', field='pass').items()
        }
      )
    return OptionValues(values, mapped.content)


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
    mission_record=mission(top=True),
    expected=False,
    artifact_view=PurePosixPath(CONTAINER_ARTIFACTS_ROOT),
    published_ports=(),
    depth=0,
  )


def _control(
  tmp_path, owner, run, *, type_class=SampleType, worker_container_spawner=None, source=None
):
  source = source or reading(
    declarations={'test': type_class.authority_declaration(frozenset({'github'}))}
  )
  host = Host()
  worker_type = type_class(host, run)
  peers = Peers(owner)
  peers.facts[owner.mission] = WorkerFacts(
    owner.type,
    owner.workspace,
    allowance=source.root_allowance(cast(str, owner.mission_record.descriptor.values['target'])),
  )
  runtime = SimpleNamespace(reference='/runtime')
  control = LaunchControl(
    ride='ride',
    types={'test': worker_type},
    peers=peers,
    journal=cast(Any, object()),
    audit_file=tmp_path / 'audit.jsonl',
    runtime_bundle=runtime,
    session_env={'ONE': '1'},
    reading=source,
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
  run = Spawn(object(), object())
  control, worker_type, peers, _ = _control(tmp_path, owner, run)
  message = _message(value=1, talk=['owner.say'])
  context = _handle(control, message)

  assert worker_type.calls == ['talk', 'launch']
  assert context.spawned[0][3] == {
    'type': 'test',
    'talk': frozenset({'worker.say', 'owner.say'}),
    'timeout': 12.0,
  }
  facts = peers.facts[message.request_id]
  assert facts.type == 'test'
  assert facts.record is not None
  assert not facts.expected


def test_type_key_is_required_before_type_owned_checks(tmp_path, owner):
  owner = replace(owner, mission_record=replace(owner.mission_record, authority=Authority()))
  control, worker_type, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(deny_talk=True))
  assert 'use rule: test:' in context.denied[0][1]
  assert worker_type.calls == []


def test_request_timeout_overrides_the_type_default(tmp_path, owner):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(timeout=3))
  assert context.spawned[0][3]['timeout'] == 3.0


def test_pass_is_authorized_before_the_type_and_reaches_the_request(tmp_path, owner):
  owner = replace(owner, mission_record=mission(top=True))
  control, worker_type, _, _ = _control(
    tmp_path,
    owner,
    Spawn(object(), object()),
    type_class=PassType,
  )

  context = _handle(control, _message(**{'pass': ['github+work']}))

  assert context.spawned
  assert worker_type.calls == ['talk', 'launch']
  assert worker_type.requests[0].passes == ('github+work',)
  assert 'pass' not in worker_type.requests[0].args


def test_pass_beyond_the_owners_rights_is_denied_before_the_type(tmp_path, owner):
  owner = replace(
    owner,
    mission_record=replace(
      owner.mission_record,
      authority=Authority.top().meet(
        Cap((Entry(Permission('test', 'cred.github', Strings(cofinite=True)), Pair()),))
      ),
    ),
  )
  control, worker_type, _, _ = _control(
    tmp_path,
    owner,
    Spawn(object(), object()),
    type_class=PassType,
  )

  context = _handle(control, _message(**{'pass': ['github+work']}))

  assert 'use rule' in context.denied[0][1]
  assert 'test.cred=github+work' in context.denied[0][1]
  assert worker_type.calls == []


@pytest.mark.parametrize(
  ('passes', 'message'),
  [
    (['github'], 'names no instance'),
    (['github+one', 'github+two'], 'twice'),
    ('github+one', 'must be a list'),
  ],
)
def test_malformed_passes_are_denied(tmp_path, owner, passes, message):
  owner = replace(
    owner,
    mission_record=mission(top=True),
  )
  control, worker_type, _, _ = _control(
    tmp_path,
    owner,
    Spawn(object(), object()),
    type_class=PassType,
  )

  context = _handle(control, _message(**{'pass': passes}))

  assert message in context.denied[0][1]
  assert worker_type.calls == []


def test_a_type_without_a_pass_field_has_no_pass_rights(tmp_path, owner):
  owner = replace(owner, mission_record=mission(top=True))
  control, worker_type, _, _ = _control(tmp_path, owner, Spawn(object(), object()))

  context = _handle(control, _message(**{'pass': ['github+work']}))

  assert 'declaration check' in context.denied[0][1]
  assert worker_type.calls == []


@pytest.mark.parametrize('worker_name', ['benchmark', 'webview'])
def test_shipped_types_without_pass_fields_deny_passes(tmp_path, owner, worker_name):
  worker_class = installed_types()[worker_name]
  host = Host()
  worker_type = worker_class(cast(Any, host))
  owner = replace(owner, mission_record=mission(top=True))
  control = LaunchControl(
    ride='ride',
    types={worker_name: worker_type},
    peers=Peers(owner),
    journal=cast(Any, object()),
    audit_file=tmp_path / 'audit.jsonl',
    runtime_bundle=SimpleNamespace(reference='/runtime'),
    session_env={},
    worker_container_spawner=cast(Spawner, object()),
    reading=reading(),
  )

  context = _handle(
    control,
    request('launch', {'type': worker_name, 'pass': ['github+work']}),
  )

  assert (
    'webview accepts cookies only' in context.denied[0][1]
    or 'unknown benchmark fields' in context.denied[0][1]
  )


@pytest.mark.parametrize('timeout', [0, -1, True, '3'])
def test_invalid_timeout_is_denied(tmp_path, owner, timeout):
  control, _, _, _ = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(timeout=timeout))
  assert 'domain check' in context.denied[0][1] and 'timeout' in context.denied[0][1]


def test_owner_is_attributed_before_type_lookup(tmp_path, owner):
  control, _, peers, _ = _control(tmp_path, owner, Spawn(object(), object()))
  peers.unattributable = True
  context = _handle(control, request('launch', {'type': 'missing'}))
  assert 'request check: missing: no owner' in context.denied[0][1]


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
  ref = 'sha256:' + 'a' * 64 + '/report.md'
  control, _, _, host = _control(tmp_path, owner, Spawn(object(), object()))
  context = _handle(control, _message(share=[ref]))
  assert context.spawned
  assert host.artifacts.resolved == [(ref, owner)]


def test_unreachable_share_is_denied(tmp_path, owner):
  ref = 'sha256:' + 'a' * 64 + '/report.md'
  control, _, _, host = _control(tmp_path, owner, Spawn(object(), object()))
  host.artifacts.denied = True
  context = _handle(control, _message(share=[ref]))
  assert 'owner cannot reach' in context.denied[0][1]


def test_container_run_hands_the_spec_and_share_to_the_host_spawner(tmp_path, owner):
  ref = 'sha256:' + 'a' * 64 + '/report.md'
  spec = WorkerContainer(
    files={'Dockerfile': b'ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n'},
    command=('worker',),
    env={},
    published_ports={8080: None},
  )
  spawner = object()
  control, _, peers, _ = _control(
    tmp_path,
    owner,
    Container(spec, extension=SampleFacts('container')),
    worker_container_spawner=spawner,
  )
  message = _message(share=[ref])
  context = _handle(control, message)

  [(launch, selected_spawner, peer, options)] = context.spawned
  assert launch.type == 'test'
  assert launch.spec is spec
  assert launch.owner_workspace == owner.workspace
  assert launch.passes == ()
  assert launch.share == (ref,)
  assert selected_spawner is spawner
  assert peer == ROOT
  assert options['type'] == 'test'
  facts = peers.facts[message.request_id]
  assert facts.extension == SampleFacts('container')
  assert facts.record is not None
  assert facts.artifact_view is None


def test_pass_is_denied_for_a_job_run(tmp_path, owner):
  owner = replace(owner, mission_record=mission(top=True))
  control, _, _, _ = _control(
    tmp_path,
    owner,
    Job(CommandJob(('true',), {})),
    type_class=PassType,
  )

  context = _handle(control, _message(**{'pass': ['github+work']}))

  assert "cannot honor 'pass'" in context.denied[0][1]


def test_container_run_carries_passes_to_the_host_spawner(tmp_path, owner):
  owner = replace(owner, mission_record=mission(top=True))
  run = Container(
    WorkerContainer(
      files={'Dockerfile': b'ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n'},
      command=('worker',),
      env={},
      published_ports={},
    )
  )
  control, _, _, _ = _control(tmp_path, owner, run, type_class=PassType)

  context = _handle(control, _message(**{'pass': ['github+work']}))

  assert context.spawned[0][0].passes == ('github+work',)


@pytest.mark.parametrize(
  'run',
  [Job(CommandJob(('true',), {})), Expect({})],
)
def test_share_is_denied_for_runs_without_a_host_workspace(tmp_path, owner, run):
  ref = 'sha256:' + 'a' * 64 + '/report.md'
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


def test_a_launch_whose_extension_view_is_not_json_is_denied(tmp_path, owner):
  control, _, peers, _ = _control(tmp_path, owner, Spawn(object(), object(), SampleFacts(object())))
  message = _message()
  [(_, error, _)] = _handle(control, message).denied
  assert 'a worker extension view is not JSON-serializable' in error
  assert peers.facts[message.request_id].extension is None


def test_audit_reads_the_current_extension_view_and_the_passes_accepted(tmp_path, owner):
  owner = replace(owner, mission_record=mission(top=True))
  message = _message(**{'pass': ['github+work']})
  control, _, peers, _ = _control(
    tmp_path,
    owner,
    Spawn(object(), object(), SampleFacts('launched')),
    type_class=PassType,
  )
  _handle(control, message)
  peers.facts[message.request_id].extension = SampleFacts('spawned')
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
  assert entry['pass'] == ['github+work']
  assert entry['published_ports'] == []
  assert entry['transition'] == 'accepted'
  assert entry['extension'] == {'detail': 'spawned'}
  assert peers.facts[message.request_id].type == 'test'


@pytest.fixture
def allowance_control(tmp_path, owner):
  from ride.bro_worker import BroType

  source = reading(
    config={
      'defaults': {'summon-depth': 2, 'grant': ['@*', 'webview']},
      'homes': {'test-home': {'bros': {'eyebro': {'summon-depth': 0}}}},
    }
  )
  owner = replace(owner, mission_record=mission(target='lead', source=source, top=True))
  control, _, peers, host = _control(tmp_path, owner, Spawn(object(), object()), source=source)
  host = SimpleNamespace(
    artifacts=host.artifacts,
    peers=peers,
    journal=object(),
    workspace=SimpleNamespace(name='ws', repository=None, metadata=SimpleNamespace(repo=None)),
    summon_spawner=object(),
    session_env={},
  )
  control._types['bro'] = BroType(cast(Any, host))
  control._types['webview'] = installed_types()['webview'](host)
  return control, peers


def _summon(control, target, **fields):
  message = request('launch', {'type': 'bro', 'target': target, 'prompt': 'work', **fields})
  context = _handle(control, message)
  return message.request_id, context


def _undertake(peers, identifier):
  facts = peers.for_mission(identifier)
  peers.owner = replace(
    peers.owner,
    mission=identifier,
    mission_record=facts.record,
    extension=facts.extension,
  )


@pytest.mark.parametrize('placement', ['boxed', 'unboxed', 'join'])
def test_allowance_chain_refuses_both_summons_and_webviews(allowance_control, placement):
  control, peers = allowance_control
  assert peers.for_mission('root').allowance.value == 2
  developer, context = _summon(control, 'bro-dev', placement=placement)
  assert context.denied == []
  assert peers.for_mission(developer).allowance.value == 1
  _undertake(peers, developer)
  reviewer, context = _summon(control, 'eyebro')
  assert context.denied == []
  assert peers.for_mission(reviewer).allowance.value == 0
  _undertake(peers, reviewer)

  for message in (
    request('launch', {'type': 'bro', 'target': 'bro-dev', 'prompt': 'work'}),
    request('launch', {'type': 'webview'}),
  ):
    context = _handle(control, message)
    [(_, error, _)] = context.denied
    assert 'allowance 0' in error
    assert 'defaults.summon-depth=2' in error
    assert 'homes.test-home.bros.eyebro.summon-depth=0' in error
    assert not context.spawned


def test_non_bro_mission_inherits_and_consumes_allowance(tmp_path, owner):
  class DelegatingType(SampleType):
    @classmethod
    def authority_declaration(cls, credential_kinds):
      return replace(
        super().authority_declaration(credential_kinds),
        base=lambda descriptor, reading: Authority.top(),
      )

  control, _, peers, _ = _control(
    tmp_path, owner, Spawn(object(), object()), type_class=DelegatingType
  )
  first = _message()
  assert not _handle(control, first).denied
  child = peers.for_mission(first.request_id)
  assert child.allowance.value == 1
  peers.owner = replace(owner, mission=first.request_id, type='test', mission_record=child.record)
  second = _message()
  assert not _handle(control, second).denied
  grandchild = peers.for_mission(second.request_id)
  assert grandchild.allowance.value == 0
  peers.owner = replace(peers.owner, mission=second.request_id, mission_record=grandchild.record)
  [(_, error, _)] = _handle(control, _message()).denied
  assert 'allowance 0' in error


def test_manual_prepare_defers_exhausted_allowance_check_to_commit(allowance_control, monkeypatch):
  control, peers = allowance_control
  identifier, context = _summon(control, 'eyebro')
  assert not context.denied
  _undertake(peers, identifier)
  token, context = _summon(control, 'bro-dev', manual=True)
  assert not context.denied
  assert context.expected
  assert peers.for_mission(token).allowance is None

  monkeypatch.setattr(pending_launch, 'claimed_workspace', lambda token: None)
  replies = []
  context = SimpleNamespace(
    journal=SimpleNamespace(records={token: SimpleNamespace(parent=identifier, state='accepted')}),
    reply=lambda peer, result: replies.append(result),
  )
  control.commit(
    context,
    'operator',
    request('operator.commit', {'token': token, 'fields': {'timeout': None}}),
  )
  [reply] = replies
  assert reply['outcome'] == 'denied'
  assert 'allowance 0' in reply['error']
  assert 'homes.test-home.bros.eyebro.summon-depth=0' in reply['error']
  assert peers.for_mission(token).allowance is None
