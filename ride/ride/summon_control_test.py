import json
from typing import Any, cast

import pytest

import ride.artifacts
import ride.bro
import ride.peer_facts
import ride.pending_summon
import ride.spawn
import ride.summon_control
from bro.base import configs
from bro.broker import brotocol
from bro.broker.dispatcher import Dispatcher
from bro.broker.journal import Journal
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.llm.llms.echo import LLMSpec as EchoLLMSpec
from bro.summon import DEFAULT_TIMEOUT
from ride.peer_facts import PeerFact, PeerFacts
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

ROOT = 'ROOT-CHANNEL'
CHILD = 'CHILD-CHANNEL'
GRANDCHILD = 'GRANDCHILD-CHANNEL'
UNKNOWN = 'UNKNOWN-CHANNEL'


@pytest.fixture(autouse=True)
def seeded_framework_bro(monkeypatch):
  from bro.registry import get_class

  monkeypatch.setattr(get_class('bro-dev'), 'may_summon', ('dev',))


class TestSummonAllowList:
  def test_seeds_from_the_bros_may_summon(self):
    assert ride.summon_control.summon_allow_list('bro-dev', grant=[], revoke=[]) == {'dev'}

  def test_grant_and_revoke_are_strict(self):
    assert ride.summon_control.summon_allow_list('bro', grant=['dev'], revoke=[]) == {'dev'}
    with pytest.raises(ValueError, match='already in the summon allow-list'):
      ride.summon_control.summon_allow_list('bro-dev', grant=['dev'], revoke=[])
    with pytest.raises(ValueError, match='not in the summon allow-list'):
      ride.summon_control.summon_allow_list('bro', grant=[], revoke=['dev'])

  def test_unknown_target_raises(self):
    with pytest.raises(ValueError, match='unknown summon target'):
      ride.summon_control.summon_allow_list('bro', grant=['not-registered'], revoke=[])


class _FakeSummonControl(ride.summon_control.SummonControl):
  test_journal: Journal

  def handle(self, context: Any, peer: Any, message: Any) -> None:
    context.active = message
    super().handle(context, peer, message)


class FakeContext:
  def __init__(self, control):
    self.control = control
    self.workers = {ROOT: 'root-quest'}
    self.replies = []
    self.spawned = []
    self.expected = []
    self.active = None
    self.journal = control.test_journal
    root = self.journal.open('root-quest', 'root', None, None, {})
    self.journal.bind(root, ROOT)

  def deny(self, peer, error):
    assert self.active is not None
    record = self.journal.deny(
      self.active.quest_id,
      'summon',
      self.workers.get(peer),
      peer,
      self.active.args,
      error,
    )
    self.replies.append((peer, {'outcome': 'denied', 'error': record.reason}))

  def spawn(self, launch, peer, *, timeout=None):
    assert self.active is not None
    self.journal.open(
      self.active.quest_id,
      'summon',
      self.workers[peer],
      peer,
      self.active.args,
    )
    self.spawned.append((launch, peer, timeout))

  def expect(self, peer, *, timeout, ready):
    assert self.active is not None
    self.journal.open(
      self.active.quest_id,
      'summon',
      self.workers[peer],
      peer,
      self.active.args,
    )
    self.expected.append((peer, timeout))
    ready(Provisioned(CHILD, Endpoint(7321, 'token')))


def _workspace(tmp_path, name='ws'):
  return Workspace.ensure(name, tmp_path, WorkspaceKind.CONTAINER)


def _control(
  tmp_path,
  allow_list=('dev',),
  credential_scope=(),
  depth_cap=configs.DEFAULT_SUMMON_DEPTH,
):
  workspace = _workspace(tmp_path)
  scope = (
    credential_scope
    if isinstance(credential_scope, ScopedSecrets)
    else ScopedSecrets(set(credential_scope), set())
  )
  facts = PeerFacts(
    PeerFact(
      workspace=workspace.name,
      bro='bro-dev',
      allow_list=frozenset(allow_list),
      credential_scope=scope,
    ),
    root_tree=workspace.tree,
    root_path=workspace.path,
  )
  journal = Journal()
  control = _FakeSummonControl(
    workspace=workspace,
    facts=facts,
    artifacts=ride.artifacts.ArtifactStore(workspace, root_in_container=True),
    journal=journal,
    audit_file=tmp_path / 'audit.jsonl',
    depth_cap=depth_cap,
  )
  control.test_journal = journal
  journal.subscribe(facts.observe_journal)
  journal.subscribe(control.observe_journal)
  journal.subscribe(control.audit_event)
  return control


def _message(**overrides):
  args = {'target': 'dev', 'prompt': 'deploy the thing', **overrides}
  return brotocol.request(
    'summon', {key: value for key, value in args.items() if value is not None}
  )


def _audit(tmp_path):
  return [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]


def test_authorized_summon_opens_identity_before_spawning(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  message = _message()
  control.handle(cast(Dispatcher, context), ROOT, message)
  [(launch, peer, timeout)] = context.spawned
  assert isinstance(launch, ride.spawn.SummonLaunchSpec)
  assert launch.target == 'dev'
  assert launch.parent == 'ws'
  assert peer == ROOT
  assert timeout == DEFAULT_TIMEOUT
  assert control._facts.for_quest(message.quest_id).bro == 'dev'
  assert not (tmp_path / 'summon-status.json').exists()
  accepted = _audit(tmp_path)[-1]
  assert accepted['transition'] == 'accepted'
  assert accepted['args']['prompt'] == 'deploy the thing'
  assert accepted['summoner'] == {'workspace': 'ws', 'bro': 'bro-dev'}


def test_audit_attributes_every_worker_backed_kind_from_journal_parent(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  record = context.journal.open('job-quest', 'benchmark', 'root-quest', ROOT, {'work': 'score'})
  context.journal.started(record)
  context.journal.end(record, {'outcome': 'ok'})
  entries = _audit(tmp_path)[-3:]
  assert [entry['transition'] for entry in entries] == ['accepted', 'started', 'ended']
  assert all(entry['summoner'] == {'workspace': 'ws', 'bro': 'bro-dev'} for entry in entries)


def test_large_summon_args_do_not_hide_the_target_from_the_audit(tmp_path, monkeypatch):
  control = _control(tmp_path)
  monkeypatch.setattr(control._artifacts, 'reachable', lambda ref, workspace: True)
  context = FakeContext(control)
  ref = 'sha256:' + 'a' * 64
  control.handle(cast(Dispatcher, context), ROOT, _message(share=[ref] * 40))
  accepted = _audit(tmp_path)[-1]
  assert accepted['args']['truncated'] is True
  assert accepted['target'] == 'dev'


def test_journal_trail_and_terminal_update_identity_audit_and_cleanup(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  message = _message()
  control.handle(cast(Dispatcher, context), ROOT, message)
  record = context.journal.records[message.quest_id]
  context.journal.bind(record, CHILD)
  context.journal.started(record)
  context.journal.trail(record, 'trail-1')
  assert control._facts.for_quest(message.quest_id).bro == 'dev'
  context.journal.end(record, {'outcome': 'failed', 'error': 'no', 'detail': {'reason': 'raised'}})
  assert [entry['transition'] for entry in _audit(tmp_path)[-3:]] == [
    'started',
    'trail',
    'ended',
  ]
  assert control._facts.for_quest(message.quest_id).bro == 'dev'
  assert not (tmp_path / 'summon-status.json').exists()


@pytest.mark.parametrize(
  ('payload', 'outcome', 'reason', 'expected_outcome'),
  [
    ({'outcome': 'ok', 'value': 'done'}, None, None, 'ok'),
    ({'outcome': 'failed', 'detail': {'reason': 'launch'}}, None, None, 'failed'),
    ({'outcome': 'failed', 'detail': {'reason': 'killed'}}, 'killed', 'killed', 'killed'),
  ],
)
def test_terminal_variants_reach_the_audit(tmp_path, payload, outcome, reason, expected_outcome):
  control = _control(tmp_path)
  context = FakeContext(control)
  message = _message()
  control.handle(cast(Dispatcher, context), ROOT, message)
  record = context.journal.records[message.quest_id]
  context.journal.end(record, payload, outcome=outcome, reason=reason)
  terminal = _audit(tmp_path)[-1]
  assert terminal['transition'] == 'ended'
  assert terminal['outcome'] == expected_outcome


def test_denial_uses_the_journal_funnel(tmp_path):
  control = _control(tmp_path, allow_list=())
  context = FakeContext(control)
  message = _message()
  control.handle(cast(Dispatcher, context), ROOT, message)
  assert context.replies[0][1]['outcome'] == 'denied'
  denial = context.journal.records[message.quest_id]
  assert denial.state == 'denied'
  assert denial.reason is not None and 'not in' in denial.reason
  assert _audit(tmp_path)[-1]['transition'] == 'denied'
  assert _audit(tmp_path)[-1]['summoner']['workspace'] == 'ws'


@pytest.mark.parametrize(
  'overrides',
  [
    {'target': ''},
    {'prompt': ''},
    {'timeout': 0},
    {'grant': [None]},
    {'unknown': True},
    {'manual': False},
  ],
)
def test_malformed_requests_are_denied(tmp_path, overrides):
  control = _control(tmp_path)
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(**overrides))
  assert context.replies[0][1]['outcome'] == 'denied'


@pytest.mark.parametrize(
  ('field', 'value'),
  [
    ('timeout', 45),
    ('into', 'feature'),
    ('hold', 'guided'),
    ('llm', '::high'),
    ('harness', 'bro'),
  ],
)
def test_launch_shape_fields_reach_the_spawn(tmp_path, field, value):
  control = _control(tmp_path)
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(**{field: value}))
  launch, _, timeout = context.spawned[0]
  if field == 'timeout':
    assert timeout == value
  else:
    assert getattr(launch, field) == value


def test_credential_overrides_reach_the_spawn(tmp_path):
  scope = ScopedSecrets({'github'}, set(), {'github': 'work'})
  control = _control(tmp_path, credential_scope=scope)
  context = FakeContext(control)
  control.handle(
    cast(Dispatcher, context),
    ROOT,
    _message(grant=['github+work'], revoke=['brave']),
  )
  launch = context.spawned[0][0]
  assert launch.grant == ('github+work',)
  assert launch.revoke == ('brave',)


def test_requested_harness_credentials_are_bounded_by_the_summoner(tmp_path):
  control = _control(tmp_path, credential_scope={'brog', 'openai'})
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(harness='claude'))
  assert 'claude_code' in context.replies[0][1]['error']


def test_requested_llm_credentials_are_bounded_by_the_summoner(tmp_path, monkeypatch):
  from bro.registry import get_class

  monkeypatch.setattr(get_class('dev'), 'llm_spec', EchoLLMSpec())
  monkeypatch.setattr(get_class('dev'), 'spells', {})
  control = _control(tmp_path, credential_scope={'brog'})
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(llm='openai:terra'))
  assert 'openai' in context.replies[0][1]['error']


def test_recipe_incompatible_with_the_harness_is_denied(tmp_path):
  control = _control(tmp_path, credential_scope={'claude_code'})
  context = FakeContext(control)
  control.handle(
    cast(Dispatcher, context),
    ROOT,
    _message(harness='claude', llm='openai:terra'),
  )
  assert 'runs Claude Code, not openai' in context.replies[0][1]['error']


def test_child_grant_bound_recomputes_its_llm_scope(tmp_path, monkeypatch):
  calls = []

  def capture_scope(target, recipe, *, attachment=None, grant, revoke, llm_spec=None):
    calls.append((target, llm_spec))
    return ScopedSecrets({'github'}, set())

  monkeypatch.setattr(ride.summon_control, 'summoned_credential_scope', capture_scope)
  control = _control(tmp_path, allow_list=('bro-dev',))
  context = FakeContext(control)
  parent = _message(target='bro-dev', llm='echo')
  control.handle(cast(Dispatcher, context), ROOT, parent)
  context.workers[CHILD] = parent.quest_id
  control._facts.note_workspace(parent.quest_id, f'broker-{CHILD}')
  calls.clear()
  control.handle(
    cast(Dispatcher, context),
    CHILD,
    _message(target='dev', grant=['github']),
  )
  assert context.spawned[-1][0].target == 'dev'
  assert calls == [('bro-dev', ride.bro.BRO.resolve_llm('echo', 'bro-dev'))]


def test_unheld_credential_instance_is_denied(tmp_path):
  scope = ScopedSecrets({'github'}, set(), {'github': 'work'})
  control = _control(tmp_path, credential_scope=scope)
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(grant=['github+other']))
  assert 'does not hold' in context.replies[0][1]['error']


def test_granting_an_unheld_bro_is_denied(tmp_path):
  control = _control(tmp_path, allow_list=('dev',))
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(grant=['@bro-dev']))
  assert 'may not summon itself' in context.replies[0][1]['error']


@pytest.mark.parametrize('override', ['@', '@missing-bro'])
def test_invalid_bro_override_is_denied(tmp_path, override):
  control = _control(tmp_path)
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(grant=[override]))
  assert context.replies[0][1]['outcome'] == 'denied'


def test_no_op_child_allow_list_override_is_denied(tmp_path):
  control = _control(tmp_path, allow_list=('bro-dev',))
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(target='bro-dev', grant=['@dev']))
  assert 'already in the summon allow-list' in context.replies[0][1]['error']


def test_unknown_and_unattributable_requesters_are_denied(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(target='not-a-bro'))
  assert 'unknown bro' in context.replies[-1][1]['error']
  control.handle(cast(Dispatcher, context), UNKNOWN, _message())
  assert 'cannot attribute' in context.replies[-1][1]['error']


def test_root_session_pointer_attributes_the_child(tmp_path):
  from bro.monitor import trail_pointer

  control = _control(tmp_path)
  trail_pointer.write(trail_pointer.session_pointer(control._workspace.path), 'root-trail')
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message())
  assert context.spawned[0][0].summoner == {'trail_id': 'root-trail'}


def test_root_trail_mark_is_the_pointer_fallback(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  root = context.journal.records['root-quest']
  context.journal.trail(root, 'root-trail')
  control.handle(cast(Dispatcher, context), ROOT, _message())
  assert context.spawned[0][0].summoner == {'trail_id': 'root-trail'}


def test_step_and_index_extend_existing_trail_provenance(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  root = context.journal.records['root-quest']
  context.journal.trail(root, 'root-trail')
  control.handle(cast(Dispatcher, context), ROOT, _message(step_id=7, index=3))
  assert context.spawned[0][0].summoner == {
    'trail_id': 'root-trail',
    'step_id': 7,
    'index': 3,
  }


def test_step_without_trail_does_not_invent_provenance(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(step_id=7))
  assert context.spawned[0][0].summoner is None


def test_nested_request_outside_the_childs_allow_list_is_denied(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  parent = _message()
  control.handle(cast(Dispatcher, context), ROOT, parent)
  context.workers[CHILD] = parent.quest_id
  control._facts.note_workspace(parent.quest_id, f'broker-{CHILD}')
  control.handle(cast(Dispatcher, context), CHILD, _message(target='bro-dev'))
  assert 'not in' in context.replies[-1][1]['error']


def test_depth_cap_denies_a_third_generation(tmp_path):
  control = _control(tmp_path, allow_list=('bro-dev',))
  context = FakeContext(control)
  first = _message(target='bro-dev')
  control.handle(cast(Dispatcher, context), ROOT, first)
  context.workers[CHILD] = first.quest_id
  control._facts.note_workspace(first.quest_id, f'broker-{CHILD}')
  second = _message(target='dev')
  control.handle(cast(Dispatcher, context), CHILD, second)
  context.workers[GRANDCHILD] = second.quest_id
  control._facts.note_workspace(second.quest_id, f'broker-{GRANDCHILD}')
  control.handle(cast(Dispatcher, context), GRANDCHILD, _message())
  assert 'depth cap' in context.replies[-1][1]['error']


def test_configured_depth_cap_controls_nested_authorization(tmp_path):
  control = _control(tmp_path, allow_list=('bro-dev',), depth_cap=1)
  context = FakeContext(control)
  first = _message(target='bro-dev')
  control.handle(cast(Dispatcher, context), ROOT, first)
  assert context.spawned[-1][0].summon_depth == 1
  context.workers[CHILD] = first.quest_id
  control._facts.note_workspace(first.quest_id, f'broker-{CHILD}')

  control.handle(cast(Dispatcher, context), CHILD, _message())

  assert 'depth cap (1) reached' in context.replies[-1][1]['error']


def test_unreachable_share_is_denied(tmp_path):
  control = _control(tmp_path)
  context = FakeContext(control)
  ref = 'sha256:' + 'a' * 64
  control.handle(cast(Dispatcher, context), ROOT, _message(share=[ref]))
  assert 'cannot share artifact' in context.replies[0][1]['error']


def test_reachable_share_reaches_the_spawn(tmp_path, monkeypatch):
  control = _control(tmp_path)
  monkeypatch.setattr(control._artifacts, 'reachable', lambda ref, workspace: True)
  context = FakeContext(control)
  ref = 'sha256:' + 'a' * 64
  control.handle(cast(Dispatcher, context), ROOT, _message(share=[ref]))
  assert context.spawned[0][0].share == (ref,)


def test_grant_is_bounded_by_the_requesters_scope(tmp_path):
  control = _control(tmp_path, credential_scope=())
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(grant=['github']))
  assert 'does not hold' in context.replies[0][1]['error']


@pytest.mark.parametrize(
  'overrides',
  [
    {'manual': True, 'timeout': 30},
    {'manual': True, 'hold': 'guided'},
    {'manual': True, 'llm': '::high'},
    {'manual': True, 'harness': 'bro'},
    {'manual': True, 'share': ['sha256:' + 'a' * 64]},
  ],
)
def test_manual_summon_refuses_host_owned_shape(tmp_path, overrides):
  control = _control(tmp_path)
  context = FakeContext(control)
  control.handle(cast(Dispatcher, context), ROOT, _message(**overrides))
  assert context.replies[0][1]['outcome'] == 'denied'


def test_manual_summon_writes_the_pending_record_before_acceptance(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  control = _control(tmp_path)
  context = FakeContext(control)
  message = _message(manual=True)
  control.handle(cast(Dispatcher, context), ROOT, message)
  assert context.spawned == []
  assert context.expected == [(ROOT, None)]
  pending = ride.pending_summon.peek(message.quest_id)
  assert pending.target == 'dev'
  assert pending.channel_token == 'token'
  assert control._facts.for_quest(message.quest_id).manual is True


def test_claimed_manual_workspace_is_the_nested_base_source(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  control = _control(tmp_path, allow_list=('bro-dev',))
  context = FakeContext(control)
  parent = _message(target='bro-dev', manual=True)
  control.handle(cast(Dispatcher, context), ROOT, parent)
  ride.pending_summon.claim(parent.quest_id, workspace='external-workspace')
  context.workers[CHILD] = parent.quest_id
  child = _message(target='dev')
  control.handle(cast(Dispatcher, context), CHILD, child)
  assert context.spawned[-1][0].parent == 'external-workspace'


def test_manual_child_cannot_grant_unattributable_credentials(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  control = _control(tmp_path, allow_list=('bro-dev',))
  context = FakeContext(control)
  parent = _message(target='bro-dev', manual=True)
  control.handle(cast(Dispatcher, context), ROOT, parent)
  ride.pending_summon.claim(parent.quest_id, workspace='external-workspace')
  context.workers[CHILD] = parent.quest_id
  control.handle(
    cast(Dispatcher, context),
    CHILD,
    _message(target='dev', grant=['github']),
  )
  assert "manual child's credential scope" in context.replies[-1][1]['error']


def test_manual_terminal_discards_pending_token(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  control = _control(tmp_path)
  context = FakeContext(control)
  message = _message(manual=True)
  control.handle(cast(Dispatcher, context), ROOT, message)
  record = context.journal.records[message.quest_id]
  context.journal.end(record, {'outcome': 'failed', 'detail': {'reason': 'disconnected'}})
  with pytest.raises(ride.pending_summon.UnknownToken):
    ride.pending_summon.peek(message.quest_id)


def test_child_uses_the_allow_list_recorded_for_its_parent(tmp_path):
  control = _control(tmp_path, allow_list=('bro-dev',))
  context = FakeContext(control)
  parent = _message(target='bro-dev')
  control.handle(cast(Dispatcher, context), ROOT, parent)
  context.workers[CHILD] = parent.quest_id
  control._facts.note_workspace(parent.quest_id, f'broker-{CHILD}')
  child = _message(target='dev')
  control.handle(cast(Dispatcher, context), CHILD, child)
  assert context.spawned[-1][0].target == 'dev'
