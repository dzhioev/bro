from dataclasses import dataclass, field
from typing import cast

import pytest

from bro.broker.brotocol import PROTOCOL_REVISION
from bro.broker.dispatcher import Dispatcher
from bro.workspace.paths import workspace_tree
from ride import pending_summon
from ride.peers import PeerIdentity, Peers, UnattributablePeer
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace

ROOT = 'ROOT-CHANNEL'
CHILD = 'CHILD-CHANNEL'
GRANDCHILD = 'GRANDCHILD-CHANNEL'


@dataclass
class _Context:
  """the two Dispatcher facts peer attribution reads."""

  root: str = ROOT
  workers: dict = field(default_factory=dict)


def _dispatcher(context: _Context) -> Dispatcher:
  return cast(Dispatcher, context)


@pytest.fixture
def peers(tmp_path):
  return Peers(Workspace.ensure('ws', tmp_path, WorkspaceKind.CONTAINER))


def _claim(quest: str, workspace: str) -> None:
  """leave the claimed record a manual child's launch writes."""
  pending_summon.write(
    pending_summon.PendingSummon(
      token=quest,
      protocol_revision=PROTOCOL_REVISION,
      port=1,
      channel_token='tk',
      target='dev',
      prompt='p',
      parent_workspace='/x',
      may_summon=(),
      grant=(),
      revoke=(),
      summoner=None,
    )
  )
  pending_summon.claim(quest, workspace=workspace)


def _spawned(peers, context, peer, quest, *, requester=ROOT) -> None:
  """register a summon the way the control and spawner do: the record at
  authorization, the workspace at spawn, the worker bind at launch resolution."""
  peers.note_summon(_dispatcher(context), requester, quest)
  peers.note_workspace(quest, f'broker-{peer}')
  context.workers[peer] = quest


class TestIdentity:
  def test_the_root_resolves_to_the_session_workspace(self, peers):
    identity = peers.identity(_dispatcher(_Context()), ROOT)
    assert identity == PeerIdentity(workspace='ws', tree=workspace_tree('ws'))

  def test_a_spawned_child_resolves_to_its_channel_named_workspace(self, peers):
    context = _Context()
    _spawned(peers, context, CHILD, 'X-1')
    identity = peers.identity(_dispatcher(context), CHILD)
    assert identity == PeerIdentity(
      workspace=f'broker-{CHILD}', tree=workspace_tree(f'broker-{CHILD}')
    )

  def test_an_unknown_peer_is_unattributable(self, peers):
    with pytest.raises(UnattributablePeer, match='cannot attribute'):
      peers.identity(_dispatcher(_Context()), CHILD)

  def test_a_job_worker_is_unattributable(self, peers):
    # a job's synthetic peer sits in the worker index with no summon record
    context = _Context(workers={'job:X-9': 'X-9'})
    with pytest.raises(UnattributablePeer, match='cannot attribute'):
      peers.identity(_dispatcher(context), 'job:X-9')

  def test_a_manual_child_is_unattributable_before_its_token_is_claimed(self, peers):
    context = _Context()
    peers.note_summon(_dispatcher(context), ROOT, 'X-1', manual=True)
    context.workers[CHILD] = 'X-1'
    with pytest.raises(UnattributablePeer, match='has not claimed its token'):
      peers.identity(_dispatcher(context), CHILD)

  def test_a_manual_child_resolves_to_its_claimed_workspace(self, peers):
    context = _Context()
    peers.note_summon(_dispatcher(context), ROOT, 'X-1', manual=True)
    context.workers[CHILD] = 'X-1'
    _claim('X-1', 'my-manual')
    identity = peers.identity(_dispatcher(context), CHILD)
    assert identity == PeerIdentity(
      workspace='my-manual', tree=workspace_tree('my-manual'), manual=True
    )

  def test_workspace_for_names_recorded_and_claimed_children(self, peers):
    context = _Context()
    _spawned(peers, context, CHILD, 'X-1')
    peers.note_summon(_dispatcher(context), ROOT, 'X-2', manual=True)
    assert peers.workspace_for('X-1') == f'broker-{CHILD}'
    assert peers.workspace_for('X-2') is None
    _claim('X-2', 'my-manual')
    assert peers.workspace_for('X-2') == 'my-manual'
    assert peers.workspace_for('X-9') is None

  def test_noting_a_summon_from_an_unattributable_requester_raises(self, peers):
    with pytest.raises(UnattributablePeer, match='cannot attribute'):
      peers.note_summon(_dispatcher(_Context()), CHILD, 'X-1')


class TestAncestors:
  def test_the_root_has_no_ancestors(self, peers):
    assert peers.ancestors(_dispatcher(_Context()), ROOT) == ()

  def test_a_root_child_ancestors_are_the_root_alone(self, peers):
    context = _Context()
    _spawned(peers, context, CHILD, 'X-1')
    assert peers.ancestors(_dispatcher(context), CHILD) == ('ws',)

  def test_a_grandchild_chain_ends_at_the_root(self, peers):
    context = _Context()
    _spawned(peers, context, CHILD, 'X-1')
    _spawned(peers, context, GRANDCHILD, 'X-2', requester=CHILD)
    assert peers.ancestors(_dispatcher(context), GRANDCHILD) == (f'broker-{CHILD}', 'ws')

  def test_the_chain_survives_a_dead_mid_chain_summoner(self, peers):
    # records outlive peers: the parent's quest closed and its worker entry
    # is gone, but the grandchild's chain still names it
    context = _Context()
    _spawned(peers, context, CHILD, 'X-1')
    _spawned(peers, context, GRANDCHILD, 'X-2', requester=CHILD)
    del context.workers[CHILD]
    assert peers.ancestors(_dispatcher(context), GRANDCHILD) == (f'broker-{CHILD}', 'ws')

  def test_an_unknown_peer_has_no_chain(self, peers):
    with pytest.raises(UnattributablePeer, match='cannot attribute'):
      peers.ancestors(_dispatcher(_Context()), CHILD)
