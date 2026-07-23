import json
from typing import cast

import pytest

import bro.launch.spawn
import bro.launch.summon_control
from broker.brotocol import Message
from broker.dispatcher import Dispatcher


class TestSummonAllowList:
  def test_seeds_from_the_bros_may_summon(self):
    # ppp-dev carries the v1 seed; a plain session summons devoops flagless
    assert bro.launch.summon_control.summon_allow_list('ppp-dev', grant=[], revoke=[]) == {
      'devoops'
    }

  def test_defaults_to_empty_for_an_unseeded_bro(self):
    assert bro.launch.summon_control.summon_allow_list('bro', grant=[], revoke=[]) == set()

  def test_grant_adds_a_registered_bro(self):
    assert bro.launch.summon_control.summon_allow_list('bro', grant=['devoops'], revoke=[]) == {
      'devoops'
    }

  def test_revoke_removes_a_seed(self):
    assert (
      bro.launch.summon_control.summon_allow_list('ppp-dev', grant=[], revoke=['devoops']) == set()
    )

  def test_grant_already_allowed_raises(self):
    with pytest.raises(ValueError, match='already in the summon allow-list'):
      bro.launch.summon_control.summon_allow_list('ppp-dev', grant=['devoops'], revoke=[])

  def test_revoke_absent_raises(self):
    with pytest.raises(ValueError, match='not in the summon allow-list'):
      bro.launch.summon_control.summon_allow_list('bro', grant=[], revoke=['devoops'])

  def test_unregistered_grant_target_raises(self):
    # registry-validated at launch: a typo fails immediately, not as a denied
    # summon minutes later
    with pytest.raises(ValueError, match='unknown summon target'):
      bro.launch.summon_control.summon_allow_list('bro', grant=['devoop'], revoke=[])

  def test_unregistered_revoke_target_raises(self):
    with pytest.raises(ValueError, match='unknown summon target'):
      bro.launch.summon_control.summon_allow_list('ppp-dev', grant=[], revoke=['devop'])

  def test_unknown_bro_degrades_to_empty_seeds_with_a_warning(self, caplog):
    # mirrors credential scoping: an ambient CW_BRO this checkout doesn't know
    # must not break the launch; explicit grants still apply on top
    result = bro.launch.summon_control.summon_allow_list(
      'no-such-bro', grant=['devoops'], revoke=[]
    )
    assert result == {'devoops'}
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


def _control(tmp_path, allow_list, session='ws') -> bro.launch.summon_control.SummonControl:
  return bro.launch.summon_control.SummonControl(
    allow_list=allow_list,
    session=session,
    project=tmp_path,
    status_file=tmp_path / 'summon-status.json',
    audit_file=tmp_path / 'audit' / 'ws.jsonl',
  )


@pytest.fixture
def control(tmp_path):
  return _control(tmp_path, {'devoops'})


def _summon_message(**overrides) -> Message:
  payload = {'target': 'devoops', 'prompt': 'deploy the thing', **overrides}
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
    assert launch == bro.launch.spawn.SummonLaunchSpec(
      target='devoops',
      prompt='deploy the thing',
      # the root's base-ref inheritance source: the bare session key names a host
      # worktree
      parent_workspace=tmp_path / 'var' / 'cw' / 'worktrees' / 'ws',
      summoner=None,
    )
    assert peer == ROOT
    assert timeout == 1800.0
    status = _status(tmp_path)
    assert [a['request_id'] for a in status['active']] == [message.id]
    assert status['active'][0]['target'] == 'devoops'
    assert status['active'][0]['trail_id'] is None
    [spawn_record] = _audit(tmp_path)
    assert spawn_record['event'] == 'spawn'
    assert spawn_record['session'] == 'ws'
    assert spawn_record['summoner'] == {'session': 'ws'}
    assert spawn_record['target'] == 'devoops'
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

  def test_container_session_key_names_the_container_workspace(self, tmp_path):
    control = _control(tmp_path, {'devoops'}, session='c:ws')
    context = FakeContext()
    # cast: FakeContext stands in for the Dispatcher surface structurally
    control.handle(cast(Dispatcher, context), ROOT, _summon_message())
    [(launch, _, _)] = context.spawned
    assert launch.parent_workspace == tmp_path / 'var' / 'cw' / 'containers' / 'ws'

  def test_child_summon_follows_its_own_seeds(self, tmp_path):
    # a summoned ppp-dev child summons devoops (ppp-dev's static seed): spawned
    # with the child as the parent, audit + status naming the child as summoner
    control = _control(tmp_path, {'ppp-dev'})
    context = FakeContext()
    request = _summon_child(control, context, CHILD, 'ppp-dev')
    control.observe_delivery(
      CHILD, ROOT, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=request.id)
    )
    child_request = _summon_message(target='devoops')
    # cast: FakeContext stands in for the Dispatcher surface structurally
    control.handle(cast(Dispatcher, context), CHILD, child_request)
    assert context.replies == []
    launch, peer, _ = context.spawned[-1]
    assert launch == bro.launch.spawn.SummonLaunchSpec(
      target='devoops',
      prompt='deploy the thing',
      # a child summoner's base-ref inheritance source: its broker-<channel> clone
      parent_workspace=tmp_path / 'var' / 'cw' / 'containers' / f'broker-{CHILD}',
      summoner={'trail_id': 'T1'},
    )
    assert peer == CHILD
    spawn_record = _audit(tmp_path)[-1]
    assert spawn_record['event'] == 'spawn'
    assert spawn_record['summoner'] == {'target': 'ppp-dev', 'trail_id': 'T1'}
    [child_active] = [a for a in _status(tmp_path)['active'] if a['request_id'] == child_request.id]
    assert child_active['summoner'] == {'target': 'ppp-dev', 'trail_id': 'T1'}

  def test_child_summon_outside_its_seeds_is_denied(self, control):
    # devoops seeds no bro, so its child summons nothing — not even a target the
    # root session itself is allowed
    context = FakeContext()
    _summon_child(control, context, CHILD, 'devoops')
    control.handle(context, CHILD, _summon_message(target='devoops'))
    assert len(context.spawned) == 1  # only the root's spawn
    [(peer, payload)] = context.replies
    assert peer == CHILD
    assert "not in devoops's may_summon seeds" in payload['error']

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
    # root (0) → ppp-dev child (1) → devoops grandchild (2): the grandchild's own
    # summon would nest to depth 3, over the cap — denied before any list check
    control = _control(tmp_path, {'ppp-dev'})
    context = FakeContext()
    _summon_child(control, context, CHILD, 'ppp-dev')
    _summon_child(control, context, GRANDCHILD, 'devoops', parent=CHILD)
    assert context.replies == []
    control.handle(cast(Dispatcher, context), GRANDCHILD, _summon_message(target='devoops'))
    assert len(context.spawned) == 2
    [(peer, payload)] = context.replies
    assert peer == GRANDCHILD
    assert 'depth cap' in payload['error']

  def test_target_outside_the_allow_list_is_denied(self, control):
    context = FakeContext()
    control.handle(context, ROOT, _summon_message(target='pm'))
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
    message = _summon_message(target='pm')
    control.handle(context, ROOT, message)
    [deny_record] = _audit(tmp_path)
    assert deny_record['event'] == 'deny'
    assert deny_record['session'] == 'ws'
    assert deny_record['request_id'] == message.id
    assert "not in this session's summon allow-list" in deny_record['reason']
    assert deny_record['summoner'] == {'session': 'ws'}
    assert deny_record['target'] == 'pm'
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
      {'target': 'devoops'},  # no prompt
      {'target': '', 'prompt': 'p'},
      {'target': 'devoops', 'prompt': 'p', 'timeout': -1},
      {'target': 'devoops', 'prompt': 'p', 'timeout': 'soon'},
      {'target': 'devoops', 'prompt': 'p', 'into': ''},
      {'target': 'devoops', 'prompt': 'p', 'timout': 60},  # typo'd key must not pass silently
      {'target': 'devoops', 'prompt': 'p', 'hold': 'automatic'},
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
    assert status['last']['target'] == 'devoops'
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
      'root exit killed in-flight child devoops' in record.getMessage()
      and 'T1' in record.getMessage()
      for record in caplog.records
    )
    assert _status(tmp_path)['active'] == []
    end_record = _audit(tmp_path)[-1]
    assert end_record['outcome'] == 'killed'
    assert end_record['trail_id'] == 'T1'
