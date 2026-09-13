from typing import cast

import pytest

from bro.broker.dispatcher import Dispatcher
from bro.broker.journal import Journal
from bro.monitor import party_member_dir
from bro.workspace.paths import workspace_tree
from ride import pending_summon
from ride.peer_facts import PeerFact, PeerFacts, PeerIdentity, UnattributablePeer
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

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
    PeerFact(
      workspace='ws',
      bro='bro-dev',
      allow_list=frozenset({'dev'}),
      credential_scope=ScopedSecrets({'github'}, set()),
    ),
    root_tree=workspace.tree,
    root_path=workspace.path,
  )
  journal = Journal()
  journal.subscribe(table.observe_journal)
  root = journal.open('root-quest', 'root', None, None, {})
  journal.bind(root, ROOT)
  context = _Context(journal)
  context.workers[ROOT] = root.quest_id
  return table, cast(Dispatcher, context), workspace


def _spawned(facts, context, peer, quest, parent, *, manual=False):
  facts.add(
    quest,
    PeerFact(
      workspace=None if manual else f'broker-{peer}',
      bro='dev',
      allow_list=frozenset(),
      manual=manual,
    ),
  )
  record = context.journal.open(quest, 'summon', parent, ROOT, {'target': 'dev'})
  context.journal.bind(record, peer)
  context.workers[peer] = quest


def test_root_row_is_seeded_by_the_host_anchored_quest(facts):
  table, context, workspace = facts
  quest, fact = table.resolve(context, ROOT)
  assert quest == 'root-quest'
  assert fact.bro == 'bro-dev'
  assert table.identity(context, ROOT) == PeerIdentity('ws', workspace.tree)
  with pytest.raises(ValueError, match='already recorded'):
    table.add('root-quest', fact)


def test_spawned_peer_resolves_through_worker_binding(facts):
  table, context, _ = facts
  _spawned(table, context, CHILD, 'child-quest', 'root-quest')
  Workspace.ensure(f'broker-{CHILD}', None, Isolation.BOXED)
  assert table.identity(context, CHILD) == PeerIdentity(
    f'broker-{CHILD}', workspace_tree(f'broker-{CHILD}')
  )
  assert table.depth(context, CHILD) == 1


def test_joined_peer_uses_the_party_tree_and_its_own_trail_pointer(facts):
  from bro.monitor import trail_pointer

  table, context, workspace = facts
  table.add(
    'child-quest',
    PeerFact('ws', 'dev', frozenset(), member='broker-CH'),
  )
  record = context.journal.open('child-quest', 'summon', 'root-quest', ROOT, {'target': 'dev'})
  context.journal.bind(record, CHILD)
  context.workers[CHILD] = 'child-quest'
  records = party_member_dir(workspace.path, 'broker-CH')
  trail_pointer.write(trail_pointer.session_pointer(records), 'member-trail')

  assert table.identity(context, CHILD) == PeerIdentity('ws', workspace.tree, member='broker-CH')
  assert table.attribution(context, CHILD) == {
    'workspace': 'ws',
    'bro': 'dev',
    'member': 'broker-CH',
    'trail_id': 'member-trail',
  }


def test_unknown_and_job_peers_are_unattributable(facts):
  table, context, _ = facts
  with pytest.raises(UnattributablePeer, match='answered quest'):
    table.identity(context, CHILD)
  context.workers['job:X'] = 'job-quest'
  with pytest.raises(UnattributablePeer, match='facts'):
    table.identity(context, 'job:X')


def test_manual_workspace_is_filled_from_its_claim(facts, monkeypatch, tmp_path):
  table, context, _ = facts
  _spawned(table, context, CHILD, 'child-quest', 'root-quest', manual=True)
  with pytest.raises(UnattributablePeer, match='has not claimed'):
    table.identity(context, CHILD)
  monkeypatch.setattr(pending_summon, 'claimed_workspace', lambda quest: 'manual-workspace')
  external_tree = tmp_path / 'manual-tree'
  external_tree.mkdir()
  Workspace.ensure('manual-workspace', None, Isolation.UNBOXED, tree=external_tree)
  assert table.identity(context, CHILD) == PeerIdentity(
    'manual-workspace', external_tree, manual=True
  )
  assert table.for_quest('child-quest').workspace == 'manual-workspace'


def test_journal_ancestry_drives_depth_and_artifact_ancestors(facts):
  table, context, _ = facts
  _spawned(table, context, CHILD, 'child-quest', 'root-quest')
  _spawned(table, context, GRANDCHILD, 'grandchild-quest', 'child-quest')
  del context.workers[CHILD]
  assert context.journal.ancestry('grandchild-quest') == ('child-quest', 'root-quest')
  assert table.depth(context, GRANDCHILD) == 2
  assert table.ancestors(context, GRANDCHILD) == (f'broker-{CHILD}', 'ws')


def test_attribution_reads_the_current_pointer_then_the_journal_fallback(facts, monkeypatch):
  from bro.monitor import trail_pointer

  table, context, workspace = facts
  root = context.journal.records['root-quest']
  context.journal.trail(root, 'journal-trail')
  assert table.attribution(context, ROOT) == {
    'workspace': 'ws',
    'bro': 'bro-dev',
    'trail_id': 'journal-trail',
  }
  trail_pointer.write(trail_pointer.session_pointer(workspace.path), 'current-trail')
  assert table.attribution(context, ROOT) == {
    'workspace': 'ws',
    'bro': 'bro-dev',
    'trail_id': 'current-trail',
  }
