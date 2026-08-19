import json
from typing import cast

import pytest

import ride.bro
import ride.scope
import ride.spawn
import ride.summon_control
from bro.broker.brotocol import Message
from bro.broker.dispatcher import Dispatcher
from bro.monitor import trail_pointer
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from bro.workspace.paths import workspace_tree
from bro.workspace.store import ScopedSecrets


@pytest.fixture(autouse=True)
def seeded_framework_bro(monkeypatch):
  from bro.registry import get_class

  monkeypatch.setattr(get_class('bro-dev'), 'may_summon', ('dev',))


class TestSummonAllowList:
  def test_seeds_from_the_bros_may_summon(self):
    assert ride.summon_control.summon_allow_list('bro-dev', grant=[], revoke=[]) == {'dev'}

  def test_defaults_to_empty_for_an_unseeded_bro(self):
    assert ride.summon_control.summon_allow_list('bro', grant=[], revoke=[]) == set()

  def test_grant_adds_a_registered_bro(self):
    assert ride.summon_control.summon_allow_list('bro', grant=['dev'], revoke=[]) == {'dev'}

  def test_revoke_removes_a_seed(self):
    assert ride.summon_control.summon_allow_list('bro-dev', grant=[], revoke=['dev']) == set()

  def test_grant_already_allowed_raises(self):
    with pytest.raises(ValueError, match='already in the summon allow-list'):
      ride.summon_control.summon_allow_list('bro-dev', grant=['dev'], revoke=[])

  def test_revoke_absent_raises(self):
    with pytest.raises(ValueError, match='not in the summon allow-list'):
      ride.summon_control.summon_allow_list('bro', grant=[], revoke=['dev'])

  def test_unregistered_grant_target_raises(self):
    # registry-validated at launch: a typo fails immediately, not as a denied
    # summon minutes later
    with pytest.raises(ValueError, match='unknown summon target'):
      ride.summon_control.summon_allow_list('bro', grant=['devoop'], revoke=[])

  def test_unregistered_revoke_target_raises(self):
    with pytest.raises(ValueError, match='unknown summon target'):
      ride.summon_control.summon_allow_list('bro-dev', grant=[], revoke=['devop'])

  def test_unknown_bro_degrades_to_empty_seeds_with_a_warning(self, caplog):
    # mirrors credential scoping: an ambient RIDE_BRO this checkout doesn't know
    # must not break the launch; explicit grants still apply on top
    result = ride.summon_control.summon_allow_list('no-such-bro', grant=['dev'], revoke=[])
    assert result == {'dev'}
    assert any('could not resolve bro' in record.message for record in caplog.records)


ROOT = 'ROOT-CHANNEL'
CHILD = 'CHILD-CHANNEL'
GRANDCHILD = 'GRANDCHILD-CHANNEL'


class FakeContext:
  """the Dispatcher surface `SummonControl.handle` drives: root exposure, the
  origin topology, plus the reply/spawn routing primitives, recorded for
  assertions."""

  def __init__(self):
    self.root = ROOT
    self.origin: dict = {}  # spawned peer -> (parent, spawning request id)
    self.replies: list = []  # (peer, payload)
    self.spawned: list = []  # (launch, peer, timeout)

  def reply(self, peer, payload):
    self.replies.append((peer, payload))

  def spawn(self, launch, peer, *, timeout=None):
    self.spawned.append((launch, peer, timeout))


def _workspace(tmp_path, name='ws') -> Workspace:
  return Workspace.ensure(name, tmp_path, WorkspaceKind.CONTAINER)


def _control(
  tmp_path, allow_list, session='ws', credential_scope=()
) -> ride.summon_control.SummonControl:
  return ride.summon_control.SummonControl(
    allow_list=allow_list,
    credential_scope=credential_scope,
    workspace=_workspace(tmp_path, session),
    status_file=tmp_path / 'summon-status.json',
    audit_file=tmp_path / 'audit' / 'ws.jsonl',
  )


@pytest.fixture
def control(tmp_path):
  return _control(tmp_path, {'dev'})


def _summon_message(**overrides) -> Message:
  payload = {'target': 'dev', 'prompt': 'deploy the thing', **overrides}
  return Message(type='summon', payload={k: v for k, v in payload.items() if v is not None})


def _summon_child(control, context, peer, target, parent=ROOT) -> Message:
  """handle an authorized summon of `target` from `parent` and register the
  spawned peer in the context topology, the way `Dispatcher._register_child`
  would once the launch resolves."""
  message = _summon_message(target=target)
  control.handle(context, parent, message)
  context.origin[peer] = (parent, message.id)
  return message


def _status(tmp_path) -> dict:
  return json.loads((tmp_path / 'summon-status.json').read_text())


def _audit(tmp_path) -> list[dict]:
  lines = (tmp_path / 'audit' / 'ws.jsonl').read_text().splitlines()
  return [json.loads(line) for line in lines]


class TestSummonHandler:
  def test_authorized_summon_spawns_with_the_default_timeout(self, control, tmp_path):
    context = FakeContext()
    message = _summon_message()
    control.handle(context, ROOT, message)
    assert context.replies == []
    [(launch, peer, timeout)] = context.spawned
    assert launch == ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      # the root's base-ref inheritance source: the bare session key names a host
      # worktree
      parent_workspace=workspace_tree(tmp_path, 'ws'),
      summoner=None,
      # dev seeds no summon targets of its own — the child is told exactly that
      may_summon=(),
    )
    assert peer == ROOT
    assert timeout == 1800.0
    status = _status(tmp_path)
    assert [a['request_id'] for a in status['active']] == [message.id]
    assert status['active'][0]['target'] == 'dev'
    assert status['active'][0]['trail_id'] is None
    [spawn_record] = _audit(tmp_path)
    assert spawn_record['event'] == 'spawn'
    assert spawn_record['session'] == 'ws'
    assert spawn_record['summoner'] == {'session': 'ws'}
    assert spawn_record['target'] == 'dev'
    assert spawn_record['prompt_head'] == 'deploy the thing'

  def test_timeout_and_into_forward_into_the_spawn(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(timeout=42, into='summon'))
    [(launch, _, timeout)] = context.spawned
    assert launch.into == 'summon'
    assert timeout == 42.0

  def test_hold_forwards_into_the_spawn(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(hold='attended'))
    [(launch, _, _)] = context.spawned
    assert launch.hold == 'attended'

  def test_the_llm_recipe_forwards_into_the_spawn(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(llm='openai:sol:high+fast'))
    [(launch, _, _)] = context.spawned
    assert launch.llm == 'openai:sol:high+fast'

  def test_the_harness_forwards_into_the_spawn_and_the_audit(self, tmp_path):
    control = _control(tmp_path, {'dev'})
    context = FakeContext()
    control.handle(cast(Dispatcher, context), ROOT, _summon_message(harness='claude'))
    [(launch, _, _)] = context.spawned
    assert launch.harness == 'claude'
    [spawn_record] = _audit(tmp_path)
    assert spawn_record['harness'] == 'claude'

  def test_credential_overrides_ride_the_spawn_and_land_in_the_audit(self, tmp_path):
    # the unified values ride the spawn (the lowering splits them); only the
    # `@bro` half resolves here
    control = _control(tmp_path, {'dev', 'bro'}, credential_scope={'aws'})
    context = FakeContext()
    # cast: FakeContext stands in for the Dispatcher surface structurally
    control.handle(
      cast(Dispatcher, context), ROOT, _summon_message(grant=['aws', '@bro'], revoke=['openai'])
    )
    [(launch, _, _)] = context.spawned
    assert launch.grant == ('aws', '@bro')
    assert launch.revoke == ('openai',)
    [spawn_record] = _audit(tmp_path)
    assert spawn_record['grant'] == ['aws', '@bro']
    assert spawn_record['revoke'] == ['openai']

  def test_granted_bro_widens_the_childs_own_allow_list(self, tmp_path):
    # dev seeds nothing, so only the grant lets its child summon bro
    control = _control(tmp_path, {'dev', 'bro'})
    context = FakeContext()
    message = _summon_message(target='dev', grant=['@bro'])
    # cast: FakeContext stands in for the Dispatcher surface structurally
    control.handle(cast(Dispatcher, context), ROOT, message)
    context.origin[CHILD] = (ROOT, message.id)
    control.handle(cast(Dispatcher, context), CHILD, _summon_message(target='bro'))
    assert context.replies == []
    assert [launch.target for launch, _, _ in context.spawned] == ['dev', 'bro']
    # the child is handed the list it is authorized against — its own, not the
    # session's, which also holds dev
    assert context.spawned[0][0].may_summon == ('bro',)

  def test_granting_a_bro_the_summoner_may_not_summon_is_denied(self, control):
    # the fixture session may summon only dev, so it cannot hand bro down
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(grant=['@bro']))
    assert context.spawned == []
    [(_, payload)] = context.replies
    assert 'may not summon itself: bro' in payload['error']

  def test_granting_a_credential_the_summoner_lacks_is_denied(self, tmp_path):
    control = _control(tmp_path, {'dev'}, credential_scope={'openai'})
    context = FakeContext()
    # cast: FakeContext stands in for the Dispatcher surface structurally
    control.handle(cast(Dispatcher, context), ROOT, _summon_message(grant=['openai', 'aws']))
    assert context.spawned == []
    [(_, payload)] = context.replies
    assert 'does not hold: aws' in payload['error']

  def test_a_childs_grants_are_bounded_by_its_own_scope(self, tmp_path):
    # the bound follows the chain: a summoned bro-dev holds github (its
    # extra_secrets) and can hand that down, but nothing it never held
    control = _control(tmp_path, {'bro-dev'})
    context = FakeContext()
    message = _summon_message(target='bro-dev')
    control.handle(cast(Dispatcher, context), ROOT, message)
    context.origin[CHILD] = (ROOT, message.id)
    control.handle(
      cast(Dispatcher, context), CHILD, _summon_message(target='dev', grant=['github'])
    )
    assert [launch.target for launch, _, _ in context.spawned] == ['bro-dev', 'dev']
    control.handle(
      cast(Dispatcher, context), CHILD, _summon_message(target='dev', grant=['gmail_creds'])
    )
    [(_, payload)] = context.replies
    assert 'does not hold: gmail_creds' in payload['error']

  def test_a_childs_grant_bound_follows_its_llm_recipe(self, tmp_path, monkeypatch):
    calls: list = []

    def capture_scope(target, recipe, *, grant, revoke, llm_spec=None):
      calls.append((target, llm_spec))
      return ScopedSecrets(required={'github'}, optional=set(), docker_sock=False)

    monkeypatch.setattr(ride.scope, 'summoned_credential_scope', capture_scope)
    control = _control(tmp_path, {'bro-dev'})
    context = FakeContext()
    message = _summon_message(target='bro-dev', llm='echo')
    control.handle(cast(Dispatcher, context), ROOT, message)
    context.origin[CHILD] = (ROOT, message.id)
    control.handle(
      cast(Dispatcher, context), CHILD, _summon_message(target='dev', grant=['github'])
    )
    assert [launch.target for launch, _, _ in context.spawned] == ['bro-dev', 'dev']
    assert calls == [('bro-dev', ride.bro.BRO.resolve_llm('echo', 'bro-dev'))]

  def test_a_claude_childs_grant_bound_follows_its_harness(self, tmp_path, monkeypatch):
    calls: list = []

    def capture_scope(target, recipe, *, grant, revoke, llm_spec=None):
      calls.append((target, recipe.name, llm_spec.TYPE if llm_spec is not None else None))
      return ScopedSecrets(required={'github'}, optional=set(), docker_sock=False)

    monkeypatch.setattr(ride.scope, 'summoned_credential_scope', capture_scope)
    control = _control(tmp_path, {'bro-dev'})
    context = FakeContext()
    message = _summon_message(target='bro-dev', harness='claude')
    control.handle(cast(Dispatcher, context), ROOT, message)
    context.origin[CHILD] = (ROOT, message.id)
    control.handle(
      cast(Dispatcher, context), CHILD, _summon_message(target='dev', grant=['github'])
    )
    assert [launch.target for launch, _, _ in context.spawned] == ['bro-dev', 'dev']
    assert calls == [('bro-dev', 'claude-full', 'claude-code')]

  def test_no_op_bro_override_is_denied(self, tmp_path):
    # bro-dev already seeds dev: the strictness of the launcher flags holds
    # on the wire too
    control = _control(tmp_path, {'bro-dev'})
    context = FakeContext()
    # cast: FakeContext stands in for the Dispatcher surface structurally
    control.handle(cast(Dispatcher, context), ROOT, _summon_message(target='bro-dev', grant=['@dev']))  # fmt: skip
    assert context.spawned == []
    [(_, payload)] = context.replies
    assert 'already in the summon allow-list' in payload['error']

  def test_unregistered_bro_override_is_denied(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(grant=['@nobody']))
    assert context.spawned == []
    [(_, payload)] = context.replies
    assert 'unknown summon target' in payload['error']

  def test_malformed_bro_override_is_denied(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(grant=['@']))
    assert context.spawned == []
    [(_, payload)] = context.replies
    assert 'malformed grant/revoke' in payload['error']

  def test_child_summon_follows_its_own_seeds(self, tmp_path):
    # a summoned bro-dev child summons dev (bro-dev's static seed): spawned
    # with the child as the parent, audit + status naming the child as summoner
    control = _control(tmp_path, {'bro-dev'})
    context = FakeContext()
    request = _summon_child(control, context, CHILD, 'bro-dev')
    control.observe_delivery(
      CHILD, ROOT, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=request.id)
    )
    child_request = _summon_message(target='dev')
    # cast: FakeContext stands in for the Dispatcher surface structurally
    control.handle(cast(Dispatcher, context), CHILD, child_request)
    assert context.replies == []
    launch, peer, _ = context.spawned[-1]
    assert launch == ride.spawn.SummonLaunchSpec(
      target='dev',
      prompt='deploy the thing',
      # a child summoner's base-ref inheritance source: its broker-<channel> clone
      parent_workspace=workspace_tree(tmp_path, f'broker-{CHILD}'),
      summoner={'trail_id': 'T1'},
      may_summon=(),
    )
    assert peer == CHILD
    spawn_record = _audit(tmp_path)[-1]
    assert spawn_record['event'] == 'spawn'
    assert spawn_record['summoner'] == {'target': 'bro-dev', 'trail_id': 'T1'}
    [child_active] = [a for a in _status(tmp_path)['active'] if a['request_id'] == child_request.id]
    assert child_active['summoner'] == {'target': 'bro-dev', 'trail_id': 'T1'}

  def test_child_source_lands_on_the_spawned_summoned_by(self, tmp_path):
    control = _control(tmp_path, {'bro-dev'})
    context = FakeContext()
    request = _summon_child(control, context, CHILD, 'bro-dev')
    control.observe_delivery(
      CHILD, ROOT, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=request.id)
    )
    control.handle(
      cast(Dispatcher, context),
      CHILD,
      _summon_message(target='dev', step_id=7, index=3),
    )
    launch, _, _ = context.spawned[-1]
    assert launch.summoner == {'trail_id': 'T1', 'step_id': 7, 'index': 3}

  def test_step_id_without_a_requester_trail_is_dropped(self, control, tmp_path):
    # the root session has no trail pointer here, so a position alone would be
    # meaningless — no summoned_by is invented for it
    del tmp_path
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(step_id=7))
    [(launch, _, _)] = context.spawned
    assert launch.summoner is None

  def test_the_session_trail_pointer_attributes_session_children(self, tmp_path):
    workspace = _workspace(tmp_path)
    pointer = trail_pointer.session_pointer(workspace.path)
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'trail_id': 'CT9'}))
    control = _control(tmp_path, {'dev'})
    context = FakeContext()
    control.handle(cast(Dispatcher, context), ROOT, _summon_message())
    [(launch, _, _)] = context.spawned
    assert launch.summoner == {'trail_id': 'CT9'}

  def test_absent_trail_pointer_degrades_to_no_summoned_by(self, tmp_path):
    # the early-launch race: the recorder has not adopted a transcript yet, so
    # the pointer file does not exist — absent provenance, never a legacy shape
    control = _control(tmp_path, {'dev'})
    context = FakeContext()
    control.handle(cast(Dispatcher, context), ROOT, _summon_message())
    [(launch, _, _)] = context.spawned
    assert launch.summoner is None

  def test_root_started_trail_attributes_a_bro_run_roots_children(self, control):
    # a bro-run root announced its trail over the broker; its summon children
    # are attributed to it (with the request's step_id when carried)
    control.note_root_trail('RT1')
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(step_id=2))
    [(launch, _, _)] = context.spawned
    assert launch.summoner == {'trail_id': 'RT1', 'step_id': 2}

  def test_child_summon_outside_its_seeds_is_denied(self, control):
    # dev seeds no bro, so its child summons nothing — not even a target the
    # root session itself is allowed
    context = FakeContext()
    _summon_child(control, context, CHILD, 'dev')
    control.handle(context, CHILD, _summon_message(target='dev'))
    assert len(context.spawned) == 1  # only the root's spawn
    [(peer, payload)] = context.replies
    assert peer == CHILD
    assert "not in dev's summon allow-list" in payload['error']

  def test_unattributable_peer_is_denied(self, control, tmp_path):
    # a peer with no origin the control can map to a spawned bro has no
    # allow-list to authorize against
    context = FakeContext()
    control.handle(context, CHILD, _summon_message())
    assert context.spawned == []
    [(peer, payload)] = context.replies
    assert peer == CHILD
    assert 'cannot attribute' in payload['error']
    [deny_record] = _audit(tmp_path)
    assert deny_record['event'] == 'deny'
    assert deny_record['summoner'] is None

  def test_depth_cap_denies_a_grandchilds_summon(self, tmp_path):
    # root (0) → bro-dev child (1) → dev grandchild (2): the grandchild's own
    # summon would nest to depth 3, over the cap — denied before any list check
    control = _control(tmp_path, {'bro-dev'})
    context = FakeContext()
    _summon_child(control, context, CHILD, 'bro-dev')
    _summon_child(control, context, GRANDCHILD, 'dev', parent=CHILD)
    assert context.replies == []
    control.handle(cast(Dispatcher, context), GRANDCHILD, _summon_message(target='dev'))
    assert len(context.spawned) == 2
    [(peer, payload)] = context.replies
    assert peer == GRANDCHILD
    assert 'depth cap' in payload['error']

  def test_target_outside_the_allow_list_is_denied(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(target='bro'))
    assert context.spawned == []
    [(_, payload)] = context.replies
    assert "not in this session's summon allow-list" in payload['error']

  def test_unknown_bro_is_denied_by_name(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(target='no-such-bro'))
    assert context.spawned == []
    [(_, payload)] = context.replies
    assert 'unknown bro' in payload['error']

  def test_denials_land_in_the_audit(self, control, tmp_path):
    context = FakeContext()
    message = _summon_message(target='bro')
    control.handle(context, ROOT, message)
    [deny_record] = _audit(tmp_path)
    assert deny_record['event'] == 'deny'
    assert deny_record['session'] == 'ws'
    assert deny_record['request_id'] == message.id
    assert "not in this session's summon allow-list" in deny_record['reason']
    assert deny_record['summoner'] == {'session': 'ws'}
    assert deny_record['target'] == 'bro'
    assert deny_record['prompt_head'] == 'deploy the thing'

  def test_deny_audit_carries_only_well_typed_payload_fields(self, control, tmp_path):
    context = FakeContext()
    control.handle(context, ROOT, Message(type='summon', payload={'target': 42}))
    [deny_record] = _audit(tmp_path)
    assert deny_record['event'] == 'deny'
    assert 'target' not in deny_record
    assert 'prompt_head' not in deny_record

  @pytest.mark.parametrize(
    'payload',
    [
      {'prompt': 'p'},  # no target
      {'target': 'dev'},  # no prompt
      {'target': '', 'prompt': 'p'},
      {'target': 'dev', 'prompt': 'p', 'timeout': -1},
      {'target': 'dev', 'prompt': 'p', 'timeout': 'soon'},
      {'target': 'dev', 'prompt': 'p', 'into': ''},
      {'target': 'dev', 'prompt': 'p', 'timout': 60},  # typo'd key must not pass silently
      {'target': 'dev', 'prompt': 'p', 'hold': 'automatic'},
      {'target': 'dev', 'prompt': 'p', 'step_id': '7'},
      {'target': 'dev', 'prompt': 'p', 'step_id': -1},
      {'target': 'dev', 'prompt': 'p', 'step_id': True},
      {'target': 'dev', 'prompt': 'p', 'index': 1},
      {'target': 'dev', 'prompt': 'p', 'step_id': 7, 'index': -1},
      {'target': 'dev', 'prompt': 'p', 'step_id': 7, 'index': True},
      {'target': 'dev', 'prompt': 'p', 'grant': 'aws'},
      {'target': 'dev', 'prompt': 'p', 'grant': ['']},
      {'target': 'dev', 'prompt': 'p', 'grant': None},  # a null cannot default to no override
      {'target': 'dev', 'prompt': 'p', 'revoke': [7]},
      {'target': 'dev', 'prompt': 'p', 'llm': '::ludicrous'},
      {'target': 'dev', 'prompt': 'p', 'llm': 'nosuchprovider'},
      {'target': 'dev', 'prompt': 'p', 'llm': 7},
      {'target': 'dev', 'prompt': 'p', 'harness': 'zsh'},
      {'target': 'dev', 'prompt': 'p', 'harness': 7},
    ],
  )
  def test_malformed_payload_is_denied(self, control, payload):
    context = FakeContext()
    control.handle(context, ROOT, Message(type='summon', payload=payload))
    assert context.spawned == []
    [(_, reply_payload)] = context.replies
    assert 'error' in reply_payload


class TestSummonLedger:
  def _spawned(self, control) -> Message:
    context = FakeContext()
    message = _summon_message()
    control.handle(context, ROOT, message)
    return message

  def test_started_records_the_trail_id(self, control, tmp_path):
    request = self._spawned(control)
    started = Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=request.id)
    control.observe_delivery(CHILD, ROOT, started)
    status = _status(tmp_path)
    assert status['active'][0]['trail_id'] == 'T1'

  def test_completed_moves_the_summon_to_last_outcome(self, control, tmp_path):
    request = self._spawned(control)
    control.observe_delivery(
      CHILD, ROOT, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=request.id)
    )
    completed = Message(
      type='completed', payload={'result': 'done', 'end_reason': 'ok'}, in_reply_to=request.id
    )
    control.observe_delivery(CHILD, ROOT, completed)
    status = _status(tmp_path)
    assert status['active'] == []
    assert status['last']['request_id'] == request.id
    assert status['last']['target'] == 'dev'
    assert status['last']['trail_id'] == 'T1'
    assert status['last']['summoner'] == {'session': 'ws'}
    assert status['last']['outcome'] == 'ok'
    end_record = _audit(tmp_path)[-1]
    assert end_record['event'] == 'end'
    assert end_record['outcome'] == 'ok'
    assert end_record['trail_id'] == 'T1'

  def test_failed_records_the_failure_reason(self, control, tmp_path):
    request = self._spawned(control)
    failed = Message(type='failed', payload={'reason': 'launch'}, in_reply_to=request.id)
    # source=None: the launch failure synthesis — no child ever existed
    control.observe_delivery(None, ROOT, failed)
    status = _status(tmp_path)
    assert status['active'] == []
    assert status['last']['outcome'] == 'failed:launch'

  def test_unrelated_deliveries_are_ignored(self, control, tmp_path):
    self._spawned(control)
    control.observe_delivery(
      CHILD, ROOT, Message(type='completed', payload={}, in_reply_to='SOME-OTHER-REQUEST')
    )
    control.observe_delivery(CHILD, ROOT, Message(type='status', payload={}))
    assert len(_status(tmp_path)['active']) == 1

  def test_root_teardown_logs_and_audits_killed_children(self, control, tmp_path, caplog):
    request = self._spawned(control)
    control.observe_delivery(
      CHILD, ROOT, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=request.id)
    )
    control.log_killed_in_flight()
    assert any(
      'root exit killed in-flight child dev' in record.getMessage() and 'T1' in record.getMessage()
      for record in caplog.records
    )
    assert _status(tmp_path)['active'] == []
    end_record = _audit(tmp_path)[-1]
    assert end_record['outcome'] == 'killed'
    assert end_record['trail_id'] == 'T1'
