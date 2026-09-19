import json

import pytest

from bro.broker import brotocol, journal as journal_module
from bro.broker.journal import Journal
from bro.broker.journal_test_helper import text_at_the_message_bound


def test_transitions_fold_the_record_and_emit_ordered_events():
  journal = Journal()
  observed = []
  journal.subscribe(lambda event, record: observed.append((event.transition, record.state)))
  record = journal.open('child', 'summon', 'root', 'requester', {'prompt': 'work'})
  journal.bind(record, 'worker')
  assert journal.started(record)
  assert journal.trail(record, 'trail-1')
  journal.end(record, {'outcome': 'ok', 'value': 'done'})
  assert [transition for transition, _ in observed] == [
    'accepted',
    'started',
    'trail',
    'ended',
  ]
  assert record.view(include_result=True)['result']['value'] == 'done'
  assert journal.head == 4


def test_a_raising_subscriber_does_not_break_the_funnel(caplog):
  journal = Journal()
  observed = []

  def raising(event, record):
    raise RuntimeError('projection broke')

  journal.subscribe(raising)
  journal.subscribe(lambda event, record: observed.append(event.transition))
  journal.open('x', 'summon', None, None, {})
  assert observed == ['accepted']
  assert 'projection broke' in caplog.text


def test_denial_is_terminal_and_remains_in_lineage():
  journal = Journal()
  record = journal.deny('x', 'summon', 'root', 'peer', {'target': 'nope'}, 'not allowed')
  assert record.view()['state'] == 'denied'
  assert record.view()['reason'] == 'not allowed'
  assert record.view(include_result=True)['result'] == {
    'outcome': 'denied',
    'error': 'not allowed',
  }
  assert journal.knows('x')


def test_events_carry_the_records_args():
  journal = Journal()
  root = journal.open('root', 'root', None, None, {})
  journal.bind(root, 'root-peer')
  child = journal.open('child', 'summon', 'root', 'root-peer', {'target': 'dev', 'prompt': 'work'})
  journal.end(child, {'outcome': 'ok'})
  denied = journal.deny('other', 'summon', 'root', 'root-peer', {'target': 'nope'}, 'not allowed')
  _, events = journal.events_after(0, 'root-peer', {'root-peer': 'root'})
  assert [(event['transition'], event['args']) for event in events] == [
    ('accepted', child.args),
    ('ended', child.args),
    ('denied', denied.args),
  ]


def test_retention_evicts_payload_then_record_but_keeps_lineage(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_RESULT_BYTES', 20)
  monkeypatch.setattr(journal_module, 'MAX_RECORDS', 1)
  journal = Journal()
  first = journal.open('one', 'job', None, None, {})
  journal.end(first, {'outcome': 'ok', 'value': 'a long answer'})
  second = journal.open('two', 'job', None, None, {})
  journal.end(second, {'outcome': 'ok', 'value': 'another long answer'})
  assert 'one' not in journal.records
  assert journal.evicted_view('one') == {
    'id': 'one',
    'kind': 'job',
    'parent': None,
    'state': 'evicted',
  }
  assert journal.records['two'].result is None
  assert journal.records['two'].result_evicted


def test_live_records_are_exempt_from_the_record_cap(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_RECORDS', 1)
  journal = Journal()
  journal.open('one', 'summon', None, None, {})
  journal.open('two', 'summon', None, None, {})
  assert set(journal.records) == {'one', 'two'}


def test_ancestry_uses_the_permanent_lineage():
  journal = Journal()
  journal.open('root', 'root', None, None, {})
  child = journal.open('child', 'summon', 'root', 'root-peer', {})
  leaf = journal.open('leaf', 'summon', 'child', 'child-peer', {})
  journal.end(child, {'outcome': 'ok'})
  journal.end(leaf, {'outcome': 'ok'})
  journal.records.pop('child')
  assert journal.ancestry('leaf') == ('child', 'root')


def test_scope_includes_only_the_callers_subtree():
  journal = Journal()
  root = journal.open('root', 'root', None, None, {})
  journal.bind(root, 'root-peer')
  left = journal.open('left', 'summon', 'root', 'root-peer', {})
  journal.bind(left, 'left-peer')
  right = journal.open('right', 'summon', 'root', 'root-peer', {})
  journal.open('leaf', 'job', 'left', 'left-peer', {})
  workers = {'root-peer': 'root', 'left-peer': 'left'}
  assert [view['id'] for view in journal.views('left-peer', workers)] == ['leaf']
  assert {view['id'] for view in journal.views('root-peer', workers)} == {
    'left',
    'right',
    'leaf',
  }
  assert not journal.visible('left-peer', left, workers)
  assert not journal.visible('left-peer', right, workers)


def test_bounded_args_keep_scalar_fields_when_containers_overflow():
  bounded = journal_module.bounded_args(
    {'target': 'dev', 'timeout': 14400, 'share': ['sha256:' + 'a' * 64] * 40}
  )
  assert bounded['target'] == 'dev'
  assert bounded['timeout'] == 14400
  assert bounded['truncated'] is True
  assert 'share' not in bounded
  encoded = json.dumps(bounded, ensure_ascii=False, separators=(',', ':')).encode()
  assert len(encoded) <= journal_module.ARGS_HEAD_BYTES


def test_bounded_args_drop_the_largest_scalars_first():
  bounded = journal_module.bounded_args({'target': 'dev', 'step_id': 10**3000, 'index': 2})
  assert bounded['target'] == 'dev'
  assert bounded['index'] == 2
  assert 'step_id' not in bounded
  assert bounded['truncated'] is True
  encoded = json.dumps(bounded, ensure_ascii=False, separators=(',', ':')).encode()
  assert len(encoded) <= journal_module.ARGS_HEAD_BYTES


def test_args_are_bounded_for_memory_and_audit():
  journal = Journal()
  record = journal.open('x', 'summon', None, None, {'prompt': 'x' * 500, 'many': list(range(1000))})
  assert len(json.dumps(record.args).encode()) <= journal_module.ARGS_HEAD_BYTES + 64


def test_trail_and_reason_text_are_bounded_in_records_and_events(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_JOURNAL_TEXT_BYTES', 10)
  journal = Journal()
  root = journal.open('root', 'root', None, None, {})
  journal.bind(root, 'root-peer')
  record = journal.open('child', 'summon', 'root', 'root-peer', {})
  journal.trail(record, 't' * 20)
  journal.end(record, {'outcome': 'failed', 'detail': {'reason': 'r' * 20}})

  view = record.view()
  assert len(view['trail_id'].encode()) <= journal_module.MAX_JOURNAL_TEXT_BYTES
  assert view['trail_id_truncated'] is True
  assert len(view['reason'].encode()) <= journal_module.MAX_JOURNAL_TEXT_BYTES
  assert view['reason_truncated'] is True
  _, events = journal.events_after(0, 'root-peer', {'root-peer': 'root'})
  assert events[-2]['trail_id_truncated'] is True
  assert events[-1]['reason_truncated'] is True


def test_journal_text_is_bounded_by_its_encoded_cost(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_JOURNAL_TEXT_BYTES', 12)
  journal = Journal()
  record = journal.open('child', 'summon', None, None, {})
  journal.end(record, {'outcome': 'failed', 'detail': {'reason': '\x00' * 20}})

  view = record.view()
  assert view['reason'] == '\x00' * 2
  assert view['reason_truncated'] is True


def test_event_gap_is_denied_but_zero_accepts_retained_history(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_EVENTS', 2)
  journal = Journal()
  root = journal.open('root', 'root', None, None, {})
  journal.bind(root, 'root-peer')
  child = journal.open('child', 'summon', 'root', 'root-peer', {})
  journal.started(child)
  journal.trail(child, 'trail')
  try:
    journal.events_after(1, 'root-peer', {'root-peer': 'root'})
  except ValueError as error:
    assert 'events gap' in str(error)
  else:
    raise AssertionError('old positive cursor was accepted')
  _, events = journal.events_after(0, 'root-peer', {'root-peer': 'root'})
  assert [event['seq'] for event in events] == [3, 4]


def test_chat_folds_bounded_tail_pending_questions_and_chat_sequence(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_RECORD_MESSAGES', 2)
  monkeypatch.setattr(journal_module, 'MAX_PENDING_QUESTIONS', 2)
  journal = Journal()
  record = journal.open(
    'child',
    'summon',
    'root',
    'requester',
    {},
    talk=frozenset({'summoner.question', 'summoned.say', 'summoned.question'}),
  )
  journal.message(record, 'summoner', brotocol.message('child', {'text': 'first'}, id='Q1'))
  first_chat_seq = record.chat_seq
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'answer'}, reply_to='Q1'))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'second'}, id='Q2'))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'third'}, id='Q3'))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'fourth'}, id='Q4'))

  assert record.chat_seq > first_chat_seq
  assert [entry['id'] for entry in record.pending] == ['Q3', 'Q4']
  assert [entry['id'] for entry in record.messages] == ['Q3', 'Q4']
  assert record.messages[-1]['from'] == 'summoned'
  assert record.messages[-1]['head'] == {'text': 'fourth'}

  journal.end(record, {'outcome': 'ok'})
  assert record.pending == []


def test_chat_state_is_folded_before_subscribers_observe_the_event():
  journal = Journal()
  record = journal.open('child', 'summon', None, None, {}, talk=frozenset({'summoned.question'}))
  observed = []
  journal.subscribe(
    lambda event, current: observed.append(
      (event.seq, current.chat_seq, list(current.messages), list(current.pending))
    )
  )

  journal.message(record, 'summoned', brotocol.message('child', {'text': 'ask'}, id='Q1'))

  [(event_sequence, chat_sequence, messages, pending)] = observed
  assert event_sequence == chat_sequence
  assert messages[-1]['id'] == 'Q1'
  assert pending[-1]['id'] == 'Q1'


def test_refusal_joins_the_tail_without_turning_a_question_pending():
  journal = Journal()
  record = journal.open('child', 'summon', 'root', 'requester', {}, talk=frozenset())
  candidate = brotocol.message('child', {'text': 'blocked'}, id='Q1', reply_to='Q0')

  journal.refused(record, 'summoned', candidate, 'summoned lacks the talk right')

  assert record.pending == []
  assert record.chat_seq == record.messages[-1]['seq']
  assert record.messages[-1]['transition'] == 'refused'
  assert record.messages[-1]['id'] == 'Q1'
  assert record.messages[-1]['reply_to'] == 'Q0'
  assert record.messages[-1]['reason'] == 'summoned lacks the talk right'
  _, events = journal.events_after(0, 'requester', {'requester': 'root'})
  assert events[-1]['transition'] == 'refused'
  assert events[-1]['id'] == 'Q1'
  assert events[-1]['reply_to'] == 'Q0'


def test_a_message_at_the_bound_is_journaled_whole():
  journal = Journal()
  record = journal.open('child', 'summon', None, None, {}, talk=frozenset({'summoned.say'}))
  payload = {'text': text_at_the_message_bound()}

  journal.message(record, 'summoned', brotocol.message('child', payload))

  assert record.messages[-1]['head'] == payload


def test_a_message_over_the_bound_is_a_journal_error():
  journal = Journal()
  record = journal.open('child', 'summon', None, None, {}, talk=frozenset({'summoned.say'}))
  payload = {'text': text_at_the_message_bound() + 'x'}

  with pytest.raises(ValueError, match='exceeds'):
    journal.message(record, 'summoned', brotocol.message('child', payload))
  assert record.messages == []


def test_a_refused_message_over_the_bound_keeps_a_marked_head():
  journal = Journal()
  record = journal.open('child', 'summon', None, None, {}, talk=frozenset())
  candidate = brotocol.message('child', {'text': text_at_the_message_bound() + 'x'})

  journal.refused(record, 'summoned', candidate, 'over the bound')

  head = record.messages[-1]['head']
  assert head['truncated'] is True
  encoded = json.dumps(head, ensure_ascii=False, separators=(',', ':')).encode()
  assert len(encoded) <= journal_module.MAX_MESSAGE_BYTES


def test_the_listing_view_keeps_pending_while_the_by_id_view_marks_the_conversation():
  journal = Journal()
  record = journal.open(
    'child', 'summon', None, None, {}, talk=frozenset({'summoned.say', 'summoned.question'})
  )
  journal.listening(record)
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'ready'}))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'go on?'}, id='Q1'))

  listing = record.view()
  by_id = record.view(include_messages=True)
  assert listing['talk'] == ['summoned.question', 'summoned.say']
  assert listing['listening'] is True
  assert [entry['id'] for entry in listing['pending']] == ['Q1']
  assert listing['chat_seq'] == record.chat_seq
  assert 'messages' not in listing
  assert 'pending' not in by_id
  assert [entry.get('id') for entry in by_id['messages']] == [None, 'Q1']
  assert 'pending' not in by_id['messages'][0]
  assert by_id['messages'][1]['pending'] is True


def test_the_tail_marks_entries_its_retention_dropped(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_RECORD_MESSAGES', 2)
  journal = Journal()
  record = journal.open('child', 'summon', None, None, {}, talk=frozenset({'summoned.say'}))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'first'}))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'second'}))
  assert 'messages_truncated' not in record.view(include_messages=True)

  journal.message(record, 'summoned', brotocol.message('child', {'text': 'third'}))

  view = record.view(include_messages=True)
  assert [entry['head']['text'] for entry in view['messages']] == ['second', 'third']
  assert view['messages_truncated'] is True
  assert 'messages_truncated' not in record.view()


def test_the_conversation_folds_an_open_question_older_than_the_tail_in_by_sequence(monkeypatch):
  monkeypatch.setattr(journal_module, 'MAX_RECORD_MESSAGES', 2)
  journal = Journal()
  record = journal.open(
    'child', 'summon', None, None, {}, talk=frozenset({'summoned.say', 'summoned.question'})
  )
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'ship?'}, id='Q1'))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'first'}))
  journal.message(record, 'summoned', brotocol.message('child', {'text': 'second'}))

  conversation = record.conversation()
  assert [entry['seq'] for entry in record.messages] == [3, 4]
  assert [(entry['seq'], entry.get('pending')) for entry in conversation] == [
    (2, True),
    (3, None),
    (4, None),
  ]
  assert conversation[0]['head'] == {'text': 'ship?'}

  journal.message(record, 'summoner', brotocol.message('child', {'text': 'yes'}, reply_to='Q1'))
  assert [entry['seq'] for entry in record.conversation()] == [4, 5]
  assert not any('pending' in entry for entry in record.conversation())


def test_own_quest_event_visibility_is_limited_to_chat_transitions():
  journal = Journal()
  root = journal.open('root', 'root', None, None, {})
  journal.bind(root, 'root-peer')
  child = journal.open('child', 'summon', 'root', 'root-peer', {}, talk=frozenset({'summoned.say'}))
  journal.bind(child, 'child-peer')
  journal.started(child)
  journal.listening(child)
  journal.message(child, 'summoned', brotocol.message('child', {'text': 'ready'}))
  journal.refused(
    child,
    'summoned',
    brotocol.message('child', {'text': 'question'}, id='Q1'),
    'not allowed',
  )

  _, events = journal.events_after(0, 'child-peer', {'root-peer': 'root', 'child-peer': 'child'})
  assert [event['transition'] for event in events] == ['listening', 'message', 'refused']
  assert journal.visible_by_id('child-peer', child, {'child-peer': 'child'})
  assert not journal.visible('child-peer', child, {'child-peer': 'child'})
