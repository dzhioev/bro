from pathlib import PurePosixPath
from typing import cast

import pytest

from bro.broker.dispatcher import Dispatcher
from bro.broker.journal import Journal
from bro.monitor import party_member_dir
from bro.worker_types import PeerDescription, UnattributablePeer
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT, workspace_tree
from ride import pending_launch
from ride.bro_worker import BroFacts
from ride.peer_facts import PeerFacts, WorkerFacts
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace

ROOT = 'ROOT-CHANNEL'
CHILD = 'CHILD-CHANNEL'
GRANDCHILD = 'GRANDCHILD-CHANNEL'


class _Context:
  def __init__(self, journal):
    self.workers = {}
    self.journal = journal


@pytest.fixture
def facts(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  workspace = Workspace.ensure('ws', tmp_path / 'repo', Isolation.BOXED)
  table = PeerFacts(
    WorkerFacts(
      type='bro',
      workspace='ws',
      tree=workspace.tree,
      launch={'bro': {'bros': frozenset({'dev'}), 'party': frozenset({'boxed'})}},
      artifact_view=PurePosixPath(CONTAINER_ARTIFACTS_ROOT),
      extension=BroFacts('bro-dev'),
    ),
    root_tree=workspace.tree,
    root_path=workspace.path,
  )
  journal = Journal()
  table.bind_journal(journal)
  journal.subscribe(table.observe_journal)
  root = journal.open('root-mission', 'root', None, None, {}, type='bro')
  journal.bind(root, ROOT)
  context = _Context(journal)
  context.workers[ROOT] = root.mission_id
  return table, cast(Dispatcher, context), workspace


def _spawned(facts, context, peer, mission, parent, *, expected=False):
  facts.add(
    mission,
    WorkerFacts(
      type='bro',
      workspace=None if expected else f'broker-{peer}',
      tree=None if expected else workspace_tree(f'broker-{peer}'),
      expected=expected,
      extension=BroFacts('dev'),
    ),
  )
  record = context.journal.open(
    mission,
    'launch',
    parent,
    ROOT,
    {'type': 'bro', 'target': 'dev'},
    type='bro',
  )
  context.journal.bind(record, peer)
  context.workers[peer] = mission


def test_root_row_is_seeded_by_the_host_anchored_mission(facts):
  table, context, workspace = facts
  description = table.resolve(context, ROOT)
  assert description == PeerDescription(
    mission='root-mission',
    workspace='ws',
    tree=workspace.tree,
    type='bro',
    bro='bro-dev',
    launch={'bro': {'bros': frozenset({'dev'}), 'party': frozenset({'boxed'})}},
    member=None,
    expected=False,
    artifact_view=PurePosixPath(CONTAINER_ARTIFACTS_ROOT),
    published_ports=(),
    depth=0,
    extension=description.extension,
  )
  with pytest.raises(ValueError, match='already recorded'):
    table.add('root-mission', table.for_mission('root-mission'))


def test_spawned_peer_resolves_through_worker_binding(facts):
  table, context, _ = facts
  _spawned(table, context, CHILD, 'child-mission', 'root-mission')
  Workspace.ensure(f'broker-{CHILD}', None, Isolation.BOXED)
  description = table.resolve(context, CHILD)
  assert description.workspace == f'broker-{CHILD}'
  assert description.tree == workspace_tree(f'broker-{CHILD}')
  assert description.depth == 1


def test_joined_peer_uses_the_party_tree_and_its_own_trail_pointer(facts):
  from bro.monitor import trail_pointer

  table, context, workspace = facts
  table.add(
    'child-mission',
    WorkerFacts(
      'bro',
      'ws',
      member='broker-CH',
      extension=BroFacts('dev'),
    ),
  )
  record = context.journal.open(
    'child-mission', 'launch', 'root-mission', ROOT, {'type': 'bro'}, type='bro'
  )
  context.journal.bind(record, CHILD)
  context.workers[CHILD] = 'child-mission'
  records = party_member_dir(workspace.path, 'broker-CH')
  trail_pointer.write(trail_pointer.session_pointer(records), 'member-trail')

  assert table.resolve(context, CHILD).tree == workspace.tree
  assert table.attribution(context, CHILD) == {
    'workspace': 'ws',
    'type': 'bro',
    'bro': 'dev',
    'member': 'broker-CH',
    'trail_id': 'member-trail',
  }


def test_unknown_and_unrecorded_peers_are_unattributable(facts):
  table, context, _ = facts
  with pytest.raises(UnattributablePeer, match='undertaken mission'):
    table.resolve(context, CHILD)
  context.workers['job:X'] = 'job-mission'
  with pytest.raises(UnattributablePeer, match='facts'):
    table.resolve(context, 'job:X')


def test_expected_workspace_is_filled_from_its_claim(facts, monkeypatch, tmp_path):
  table, context, _ = facts
  _spawned(table, context, CHILD, 'child-mission', 'root-mission', expected=True)
  with pytest.raises(UnattributablePeer, match='has not claimed'):
    table.resolve(context, CHILD)
  monkeypatch.setattr(pending_launch, 'claimed_workspace', lambda mission: 'manual-workspace')
  external_tree = tmp_path / 'manual-tree'
  external_tree.mkdir()
  Workspace.ensure('manual-workspace', None, Isolation.UNBOXED, tree=external_tree)
  description = table.resolve(context, CHILD)
  assert description.workspace == 'manual-workspace'
  assert description.tree == external_tree
  assert description.expected
  assert table.for_mission('child-mission').workspace == 'manual-workspace'


def test_journal_ancestry_drives_depth_and_artifact_ancestors(facts):
  table, context, _ = facts
  _spawned(table, context, CHILD, 'child-mission', 'root-mission')
  _spawned(table, context, GRANDCHILD, 'grandchild-mission', 'child-mission')
  del context.workers[CHILD]
  assert table.resolve(context, GRANDCHILD).depth == 2
  assert table.ancestors(context, GRANDCHILD) == (f'broker-{CHILD}', 'ws')


def test_attribution_reads_the_current_pointer_then_the_journal_fallback(facts):
  from bro.monitor import trail_pointer

  table, context, workspace = facts
  root = context.journal.records['root-mission']
  context.journal.trail(root, 'journal-trail')
  assert table.attribution(context, ROOT)['trail_id'] == 'journal-trail'
  trail_pointer.write(trail_pointer.session_pointer(workspace.path), 'current-trail')
  assert table.attribution(context, ROOT)['trail_id'] == 'current-trail'
