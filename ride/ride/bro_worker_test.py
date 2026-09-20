from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import ride.bro_worker as bro_worker
from bro.broker.brotocol import Talk
from bro.worker_types import Expect, LaunchDenied, LaunchRequest, PeerDescription, Spawn
from ride.bro_worker import BroFacts, BroType, Placement, SummonLaunchSpec
from ride.workspace.metadata import Isolation
from ride.workspace.store import ScopedSecrets


class Peers:
  def attribution_for_mission(self, journal, mission):
    return {'workspace': 'ws', 'type': 'bro', 'bro': 'bro-dev', 'trail_id': 'trail'}


class Host:
  def __init__(self):
    self.depth_cap = 2
    self.summon_harness = 'bro'
    self.workspace = SimpleNamespace(
      name='ws',
      metadata=SimpleNamespace(repo=None),
      repository=None,
    )
    self.peers = Peers()
    self.journal = object()
    self.session_env = {'ONE': '1'}
    self.summon_spawner = object()


def _owner(
  tmp_path: Path,
  *,
  type: str = 'bro',
  allow_list=('dev',),
  permits=('bro.party.start.boxed',),
  depth=0,
  credential_scope=(),
):
  extension = BroFacts(
    bro='bro-dev',
    allow_list=frozenset(allow_list),
    credential_scope=ScopedSecrets(set(credential_scope), set()),
  )
  return PeerDescription(
    mission='root',
    workspace='ws',
    tree=tmp_path,
    type=type,
    bro='bro-dev' if type == 'bro' else None,
    permits=frozenset(permits),
    member=None,
    expected=False,
    artifact_view=True,
    published_ports=(),
    depth=depth,
    extension=extension if type == 'bro' else None,
  )


def _request(tmp_path, *, owner=None, manual=False, share=(), talk=(), **args):
  return LaunchRequest(
    id='mission',
    type='bro',
    args={'target': 'dev', 'prompt': 'work', **args},
    owner=_owner(tmp_path) if owner is None else owner,
    requested_talk=cast(Talk, frozenset(talk)),
    timeout=None if manual else 1800.0,
    share=tuple(share),
    manual=manual,
  )


@pytest.fixture(autouse=True)
def launch_scope(monkeypatch):
  monkeypatch.setattr(bro_worker, 'configured_scope_layers', lambda *args, **kwargs: ())
  monkeypatch.setattr(
    bro_worker,
    'summon_allow_list',
    lambda target, **kwargs: {'reviewer'} if target == 'dev' else set(),
  )
  monkeypatch.setattr(
    bro_worker,
    'effective_permits',
    lambda layers, *, grant, revoke, strict: {
      'bro.party.start.boxed',
      *[value.removeprefix(':') for value in grant if value.startswith(':')],
    },
  )
  monkeypatch.setattr(bro_worker, '_credential_refusal', lambda *args, **kwargs: None)
  monkeypatch.setattr(bro_worker, 'workspace_isolation', lambda name: Isolation.BOXED)


def test_talk_widens_the_bro_default(tmp_path):
  worker = BroType(Host())
  assert worker.talk(_request(tmp_path, talk=('owner.say',))) == frozenset(
    {'worker.say', 'owner.say'}
  )


def test_spawn_launch_carries_the_authorized_child(tmp_path):
  host = Host()
  run = BroType(host).launch(_request(tmp_path, hold='guided', share=('sha256:' + 'a' * 64,)))
  assert isinstance(run, Spawn)
  assert run.spawner is host.summon_spawner
  assert run.extension == BroFacts(
    bro='dev',
    allow_list=frozenset({'reviewer'}),
    placement=Placement('start', Isolation.BOXED),
  )
  assert run.permits == frozenset({'bro.party.start.boxed'})
  launch = cast(SummonLaunchSpec, run.launch)
  assert launch.target == 'dev'
  assert launch.parent == 'ws'
  assert launch.parent_tree == tmp_path
  assert launch.hold == 'guided'
  assert launch.share == ('sha256:' + 'a' * 64,)
  assert launch.summoner == {'trail_id': 'trail'}
  assert launch.env == {'ONE': '1'}


def test_manual_launch_returns_the_type_owned_pending_extension(tmp_path):
  run = BroType(Host()).launch(_request(tmp_path, manual=True, into='feature'))
  assert isinstance(run, Expect)
  assert run.pending == {
    'target': 'dev',
    'prompt': 'work',
    'may_summon': ['reviewer'],
    'permits': ['bro.party.start.boxed'],
    'grant': [],
    'revoke': [],
    'summoner': {'trail_id': 'trail'},
    'repo': None,
    'into': 'feature',
  }
  assert cast(BroFacts, run.extension).placement == Placement('start', None)


@pytest.mark.parametrize(
  ('args', 'message'),
  [
    ({'target': ''}, 'non-empty string'),
    ({'prompt': ''}, 'non-empty string'),
    ({'unknown': True}, 'unknown bro launch field'),
    ({'party': 'merge'}, "must be 'start' or 'join'"),
    ({'isolation': 'shared'}, "must be 'boxed' or 'unboxed'"),
    ({'grant': [None]}, 'list of non-empty names'),
    ({'step_id': -1}, 'non-negative int'),
    ({'index': 0}, 'requires step_id'),
  ],
)
def test_malformed_bro_arguments_are_denied(tmp_path, args, message):
  with pytest.raises(LaunchDenied, match=message):
    BroType(Host()).launch(_request(tmp_path, **args))


def test_owner_of_another_type_is_denied(tmp_path):
  with pytest.raises(LaunchDenied, match='another type'):
    BroType(Host()).launch(_request(tmp_path, owner=_owner(tmp_path, type='test')))


def test_depth_cap_is_enforced(tmp_path):
  with pytest.raises(LaunchDenied, match='depth cap'):
    BroType(Host()).launch(_request(tmp_path, owner=_owner(tmp_path, depth=2)))


def test_target_must_be_in_the_owners_allow_list(tmp_path):
  with pytest.raises(LaunchDenied, match='summon allow-list'):
    BroType(Host()).launch(_request(tmp_path, owner=_owner(tmp_path, allow_list=())))


def test_unmarked_placement_prefers_boxed_then_unboxed(tmp_path):
  worker = BroType(Host())
  boxed = worker.launch(
    _request(
      tmp_path,
      owner=_owner(
        tmp_path,
        permits=('bro.party.start.boxed', 'bro.party.start.unboxed'),
      ),
    )
  )
  assert cast(BroFacts, boxed.extension).placement == Placement('start', Isolation.BOXED)
  unboxed = worker.launch(
    _request(
      tmp_path,
      owner=_owner(tmp_path, permits=('bro.party.start.unboxed',)),
    )
  )
  assert cast(BroFacts, unboxed.extension).placement == Placement('start', Isolation.UNBOXED)


def test_join_needs_its_permit_and_inherits_isolation(tmp_path):
  worker = BroType(Host())
  with pytest.raises(LaunchDenied, match='bro.party.join'):
    worker.launch(_request(tmp_path, party='join'))
  run = worker.launch(
    _request(
      tmp_path,
      owner=_owner(tmp_path, permits=('bro.party.join',)),
      party='join',
    )
  )
  assert cast(BroFacts, run.extension).placement == Placement('join', Isolation.BOXED)
  assert cast(SummonLaunchSpec, cast(Spawn, run).launch).isolation is None


def test_explicit_isolation_needs_its_permit(tmp_path):
  with pytest.raises(LaunchDenied, match='bro.party.start.unboxed'):
    BroType(Host()).launch(_request(tmp_path, isolation='unboxed'))


def test_granted_bros_and_permits_must_be_held(tmp_path):
  worker = BroType(Host())
  with pytest.raises(LaunchDenied, match='may not summon itself'):
    worker.launch(_request(tmp_path, grant=['@reviewer']))
  with pytest.raises(LaunchDenied, match='does not hold'):
    worker.launch(_request(tmp_path, grant=[':bro.party.start.unboxed']))


def test_audit_fields_are_projected_from_bro_facts(tmp_path):
  worker = BroType(Host())
  run = worker.launch(_request(tmp_path))
  assert worker.audit_fields(run.extension) == {
    'placement': {'party': 'start', 'isolation': 'boxed'},
    'target': 'dev',
  }
