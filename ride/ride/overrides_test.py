import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bro.authority.authority import Atom
from bro.authority.patterns import Descriptor
from bro.authority.records import dump_request, load_authority
from bro.authority.rules import Override
from bro.broker.journal import Journal
from bro.monitor import party_member_dir
from ride import overrides
from ride.authority_launch import mission_view
from ride.authority_test_helper import mission, reading
from ride.peer_facts import PeerFacts, WorkerFacts
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


@pytest.fixture
def running():
  workspace = Workspace.ensure('root', None, Isolation.UNBOXED)
  source = reading()
  original = mission(source=source, revoke=['@bro'])
  facts = WorkerFacts('bro', workspace.name, record=original)
  peers = PeerFacts(facts, root_tree=workspace.tree, root_path=workspace.path)
  journal = Journal()
  peers.bind_journal(journal)
  journal.subscribe(peers.observe_journal)
  record = journal.open('root-id', 'root', None, None, {}, type='bro')
  journal.bind(record, 'worker')
  journal.started(record)
  workspace.resume_file.write_text(
    json.dumps({'authority_request': dump_request(original.request, original.descriptor)})
  )
  return SimpleNamespace(
    workspace=workspace,
    source=source,
    original=original,
    facts=facts,
    peers=peers,
    journal=journal,
    record=record,
  )


def apply(running, *changes):
  return overrides.apply(
    running.peers, running.journal, running.source, {'id': 'root-id', 'changes': list(changes)}
  )


def test_latest_flag_wins_above_starter_cap_and_revoke_refuses_the_next_start(running):
  types = tuple(running.source.installation.declarations)
  descriptor = Descriptor('bro', {'target': 'bro'})
  assert not running.original.authority.evaluate(
    Atom('bro', 'target', 'bro'), descriptor, types
  ).use.matches(descriptor)
  first = apply(running, ('grant', '@bro'), ('revoke', '@bro'), ('grant', '@bro'))
  assert first['status'] == 'applied'
  assert 'bro' in mission_view(running.facts.record)['may_summon']
  assert running.facts.pending_override is None
  saved = json.loads(running.workspace.resume_file.read_text())
  assert saved['override_operation']['id'] == first['operation']
  gift = load_authority(saved['operator_gift'], running.source.syntax)
  assert gift.evaluate(Atom('bro', 'target', 'bro'), descriptor, types).use.matches(descriptor)
  second = apply(running, ('revoke', '@bro'))
  assert first['operation'] != second['operation']
  assert 'bro' not in mission_view(running.facts.record)['may_summon']
  assert running.original.operator_gift.entries == ()
  assert [event['operation'] for event in running.record.overrides] == [
    first['operation'],
    second['operation'],
  ]
  head, events = running.journal.events_after(0, 'worker', {'worker': 'root-id'})
  assert head == running.journal.head
  assert [event['transition'] for event in events] == ['override', 'override']


def test_pending_candidate_is_recorded_before_persistence_and_blocks_another_override(
  running, monkeypatch
):
  write = overrides.atomic_record
  observed = []

  def persist(path, data):
    observed.append(running.facts.pending_override['id'])
    assert running.facts.record is running.original
    with pytest.raises(ValueError, match='operation .* is pending'):
      apply(running, ('revoke', '@bro'))
    write(path, data)

  monkeypatch.setattr(overrides, 'atomic_record', persist)
  result = apply(running, ('grant', '@bro'))
  assert observed == [result['operation']]


def test_failed_resume_write_leaves_authority_journal_and_record_unchanged(running, monkeypatch):
  saved = running.workspace.resume_file.read_bytes()
  head = running.journal.head

  def fail(*args):
    raise OSError('read-only record')

  monkeypatch.setattr(overrides.os, 'replace', fail)
  with pytest.raises(OSError, match='read-only record'):
    apply(running, ('grant', '@bro'))
  assert running.facts.record is running.original
  assert running.facts.pending_override is None
  assert running.workspace.resume_file.read_bytes() == saved
  assert running.journal.head == head
  assert not list(running.workspace.path.glob('.override-*'))


def test_member_operation_uses_member_records_and_never_rewrites_party_resume(running):
  member = party_member_dir(running.workspace.path, 'member')
  member.mkdir(parents=True)
  running.facts.member = 'member'
  saved = running.workspace.resume_file.read_bytes()
  result = apply(running, ('grant', '@bro'))
  assert json.loads((member / 'override.json').read_text())['id'] == result['operation']
  assert running.workspace.resume_file.read_bytes() == saved
  assert not (member / 'resume.json').exists()


def test_nonresumable_mission_can_be_overridden_without_creating_resume(running):
  running.workspace.resume_file.unlink()
  assert apply(running, ('grant', '@bro'))['status'] == 'applied'
  assert not running.workspace.resume_file.exists()


@pytest.mark.parametrize(
  'changes',
  [
    [],
    [('grant', 'github')],
    [('grant', '@*')],
    [('revoke', 'bro.placement=join')],
    [('grant', '@bro to *')],
    [('grant', '@bro'), ('unknown', '@bro')],
  ],
)
def test_names_outside_the_override_surface_are_refused(running, changes):
  with pytest.raises(ValueError, match='override check'):
    apply(running, *changes)
  assert running.facts.record is running.original


def test_operator_gift_rule_is_checked_before_existence(running):
  source = reading(config={'operator': {'revoke': ['@missing-target']}})
  running.source = source
  running.facts.record = replace(running.original, reading=source)
  with pytest.raises(ValueError, match='gift rule.*@missing-target'):
    apply(running, ('grant', '@missing-target'))
  with pytest.raises(ValueError, match='existence check.*missing-target'):
    apply(running, ('revoke', '@missing-target'))


def test_operator_start_atom_is_checked_even_for_a_revoke(running):
  running.source = reading(config={'operator': {'revoke': ['bro']}})
  with pytest.raises(ValueError, match='use rule: bro:'):
    apply(running, ('revoke', '@bro'))


def test_ended_mission_cannot_be_overridden(running):
  running.journal.end(running.record, {'outcome': 'ok'})
  with pytest.raises(ValueError, match='not a running bro session'):
    apply(running, ('grant', '@bro'))


def test_resume_layer_restores_the_committed_authority(running):
  apply(running, ('grant', '@bro'))
  saved = json.loads(running.workspace.resume_file.read_text())
  resumed = running.source.rules.override(
    running.original,
    Override(
      gift=load_authority(saved['operator_gift'], running.source.syntax),
      cap=load_authority(saved['operator_cap'], running.source.syntax),
    ),
    running.source.operator(),
  )
  assert 'bro' in mission_view(resumed)['may_summon']
