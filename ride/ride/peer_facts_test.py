from pathlib import PurePosixPath
from typing import cast
from unittest.mock import MagicMock

import pytest

from bro.broker.dispatcher import Dispatcher
from bro.broker.journal import Journal
from bro.monitor import party_member_dir
from bro.worker_types import PeerDescription, UnattributablePeer
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT, workspace_tree
from ride import pending_launch
from ride.authority_test_helper import mission, reading
from ride.bro_worker import BroFacts
from ride.launch_control import LaunchControl
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
      record=mission(grant=('@dev',)),
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
    mission_record=table.for_mission('root-mission').record,
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


def test_operator_listing_names_workspaces_members_and_unplaced_missions(facts, monkeypatch):
  table, context, workspace = facts
  _spawned(table, context, CHILD, 'child-mission', 'root-mission')
  _spawned(table, context, GRANDCHILD, 'expected-mission', 'root-mission', expected=True)
  table.add('joined-mission', WorkerFacts('bro', workspace.name, member='member-name'))
  context.journal.open('joined-mission', 'launch', 'root-mission', ROOT, {}, type='bro')
  table.add('job-mission', WorkerFacts('benchmark', None))
  context.journal.open('job-mission', 'launch', 'root-mission', ROOT, {}, type='benchmark')
  monkeypatch.setattr(pending_launch, 'claimed_workspace', lambda _mission: None)

  rows = {row['id']: row for row in table.missions()}
  assert rows['root-mission']['name'] == workspace.name
  assert rows['child-mission']['name'] == f'broker-{CHILD}'
  assert rows['joined-mission']['name'] == 'member-name'
  assert rows['joined-mission']['workspace'] == workspace.name
  assert rows['joined-mission']['member'] == 'member-name'
  assert rows['expected-mission']['workspace'] is None
  assert rows['expected-mission']['name'] == 'expected-mission'
  assert rows['job-mission']['name'] == 'job-mission'
  assert rows['job-mission']['type'] == 'benchmark'

  monkeypatch.setattr(pending_launch, 'claimed_workspace', lambda _mission: 'manual-workspace')
  claimed = {row['id']: row for row in table.missions()}
  assert claimed['expected-mission']['name'] == 'manual-workspace'


@pytest.mark.parametrize('attached', [False, True])
def test_first_listing_after_manual_worker_ends_retains_claimed_workspace(facts, attached):
  table, context, workspace = facts
  mission = 'manual-mission'
  table.add(mission, WorkerFacts('bro', None, expected=True))
  record = context.journal.open(mission, 'launch', 'root-mission', ROOT, {}, type='bro')
  control = LaunchControl(
    reading=reading(),
    ride=workspace.name,
    types={},
    peers=table,
    journal=context.journal,
    audit_file=workspace.path / 'audit.jsonl',
    runtime_bundle=MagicMock(),
    session_env={},
    worker_container_spawner=MagicMock(),
  )
  context.journal.subscribe(control.observe_journal)
  pending_launch.write(
    pending_launch.PendingLaunch(
      token=mission,
      runtime='/runtime',
      port=1234,
      channel_token='secret',
      type='bro',
      talk=(),
      owner_tree=str(workspace.tree),
    )
  )
  pending_launch.claim(mission, workspace='manual-workspace')
  if attached:
    context.journal.bind(record, CHILD)
    context.journal.started(record)
    assert table.for_mission(mission).workspace == 'manual-workspace'
  context.journal.end(record, {'outcome': 'ok'})

  assert pending_launch.claimed_workspace(mission) is None
  [row] = [row for row in table.missions() if row['id'] == mission]
  assert row['state'] == 'ended'
  assert row['workspace'] == 'manual-workspace'
  assert row['name'] == 'manual-workspace'
