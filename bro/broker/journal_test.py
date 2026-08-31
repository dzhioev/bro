import json

from bro.broker import journal as journal_module
from bro.broker.journal import Journal


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


def test_args_are_bounded_for_memory_and_audit():
  journal = Journal()
  record = journal.open('x', 'summon', None, None, {'prompt': 'x' * 500, 'many': list(range(1000))})
  assert len(json.dumps(record.args).encode()) <= journal_module.ARGS_HEAD_BYTES + 64


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
