import asyncio
import os
import signal
import threading
from pathlib import Path
from typing import Optional, cast

import pytest

from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.dispatcher import (
  CANCEL,
  EVENTS,
  PING,
  QUERY,
  TERMINATED_EXIT_CODE,
  Dispatcher,
  cancel_handler,
  events_handler,
  ping_handler,
  query_handler,
)
from bro.broker.job import CommandJob
from bro.broker.journal import (
  ARGS_HEAD_BYTES,
  ARGS_STRING_HEAD,
  MAX_MESSAGE_BYTES,
  MAX_PENDING_QUESTIONS,
  MAX_RECORD_MESSAGES,
)
from bro.broker.journal_test_helper import text_at_the_message_bound
from bro.broker.runtime import Runtime
from bro.broker.spawn import LaunchSpec, Spawner
from bro.broker.supervisor import LAUNCH_TIMEOUT, Scheduler, call_later
from bro.broker.supervisor_test_helper import FakeScheduler
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint


class FakeHandle:
  def __init__(self, release: Optional[asyncio.Event] = None):
    self.exit = asyncio.get_running_loop().create_future()
    self.killed = False
    self.release = release

  async def wait(self):
    return await self.exit

  async def kill(self):
    self.killed = True
    if self.release is not None:
      await self.release.wait()
    if not self.exit.done():
      self.exit.set_result(-15)

  def output_tail(self):
    return 'output'


class FakeRuntime:
  def __init__(self):
    self.sent = []
    self.events = {}
    self.handle = None
    self.handles: dict[str, FakeHandle] = {}
    self.stopped = False
    self.launch_messages = []
    self.launch_error: Optional[Exception] = None
    self.provision_error: Optional[Exception] = None
    self.kill_release: Optional[asyncio.Event] = None
    self.launch_gate: Optional[asyncio.Event] = None

  async def provision(self, events):
    if self.provision_error is not None:
      raise self.provision_error
    channel = f'worker-{len(self.events) + 1}'
    self.events[channel] = events
    return Provisioned(channel, Endpoint(1234, f'token-{channel}'))

  async def spawn(self, launch, provisioned, mission, talk):
    if self.launch_error is not None:
      raise self.launch_error
    if self.launch_gate is not None:
      await self.launch_gate.wait()
    self.handle = FakeHandle(self.kill_release)
    self.handles[mission] = self.handle
    for message in self.launch_messages:
      self.events[provisioned.channel].on_message(message)
    return self.handle

  async def launch_job(self, command, directory: Path):
    self.handle = FakeHandle()
    return self.handle

  def send(self, peer, message):
    self.sent.append((peer, message))

  async def close(self, peer):
    self.events.pop(peer, None)

  async def serve(self):
    await asyncio.Future()

  async def stop(self):
    self.stopped = True


async def _settle():
  for _ in range(20):
    await asyncio.sleep(0)


def _request(kind, args, quest):
  return Message(type=Tag.REQUEST, id=quest, payload={'kind': kind, 'args': args})


def _spawn_handler(spawner: Spawner):
  def handle(context, peer, message):
    context.spawn(LaunchSpec(), spawner, peer, talk=frozenset(), type='bro', timeout=None)

  return handle


def _dispatcher(*, job_output=None, schedule: Scheduler = call_later):
  runtime = FakeRuntime()
  dispatcher = Dispatcher(job_output=job_output, schedule=schedule)
  dispatcher.bind(cast(Runtime, runtime))
  root = dispatcher.journal.open('root-quest', 'root', None, None, {}, type='bro')
  dispatcher.journal.bind(root, 'requester')
  dispatcher.workers['requester'] = 'root-quest'
  return dispatcher, runtime


def _maximum_frame_result_payload() -> dict:
  value = 'x' * brotocol.MAX_FRAME_BYTES
  message = Message(type=Tag.RESULT, payload={'outcome': 'ok', 'value': value}, request='child')
  overflow = len(message.to_bytes()) - brotocol.MAX_FRAME_BYTES
  payload = {'outcome': 'ok', 'value': value[:-overflow]}
  assert (
    len(Message(type=Tag.RESULT, payload=payload, request='child').to_bytes())
    <= brotocol.MAX_FRAME_BYTES
  )
  return payload


def _maximum_frame_request() -> Message:
  target = 'x' * brotocol.MAX_FRAME_BYTES
  message = _request('work', {'target': target}, 'request')
  overflow = len(message.to_bytes()) - brotocol.MAX_FRAME_BYTES
  fitted = _request('work', {'target': target[:-overflow]}, 'request')
  assert len(fitted.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  return fitted


def test_ping_answers_inline_without_journaling_the_read():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(PING, ping_handler)
  request = _request(PING, {'value': 3}, 'ping-request')
  dispatcher.on_message('requester', request)
  assert runtime.sent[-1][1].payload == {'outcome': 'ok', 'value': {'value': 3}}
  assert not dispatcher.journal.knows('ping-request')


def test_unknown_kind_and_lineage_collision_are_denied_without_records():
  dispatcher, runtime = _dispatcher()
  dispatcher.on_message('requester', _request('missing', {}, 'unknown'))
  dispatcher.on(PING, ping_handler)
  dispatcher.on_message('requester', _request(PING, {}, 'root-quest'))
  assert [message.payload['outcome'] for _, message in runtime.sent] == ['denied', 'denied']
  assert not dispatcher.journal.knows('unknown')


def test_a_raising_handler_is_denied_and_the_next_request_is_served(caplog):
  dispatcher, runtime = _dispatcher()

  def crash(context, peer, message):
    raise FileNotFoundError('persona.md')

  dispatcher.on('work', crash)
  dispatcher.on(PING, ping_handler)

  dispatcher.on_message('requester', _request('work', {}, 'crashed'))

  denial = runtime.sent[-1][1]
  assert denial.type == Tag.RESULT
  assert denial.request_id == 'crashed'
  assert denial.outcome == 'denied'
  assert 'FileNotFoundError' in denial.payload['error']
  assert 'persona.md' in denial.payload['error']
  assert not dispatcher.journal.knows('crashed')
  assert 'Traceback' in caplog.text

  dispatcher.on_message('requester', _request(PING, {}, 'after'))
  assert runtime.sent[-1][1].payload == {'outcome': 'ok', 'value': {}}


@pytest.mark.asyncio
async def test_a_handler_raising_after_opening_its_record_gets_no_second_answer(caplog):
  dispatcher, runtime = _dispatcher()

  def open_then_crash(context, peer, message):
    context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), type='bro', timeout=None
    )
    raise RuntimeError('after open')

  dispatcher.on('work', open_then_crash)

  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()

  assert 'after open' in caplog.text
  assert 'work' in dispatcher.live
  assert [message.type for _, message in runtime.sent] == ['mark', 'mark']


def test_a_handler_raising_after_replying_gets_no_second_answer(caplog):
  dispatcher, runtime = _dispatcher()

  def reply_then_crash(context, peer, message):
    context.reply(peer, {'outcome': 'ok'})
    raise RuntimeError('after reply')

  dispatcher.on('work', reply_then_crash)

  dispatcher.on_message('requester', _request('work', {}, 'work'))

  assert 'after reply' in caplog.text
  assert [message.payload for _, message in runtime.sent] == [{'outcome': 'ok'}]


@pytest.mark.asyncio
async def test_a_handler_raising_after_deferring_its_answer_gets_no_second_answer(caplog):
  dispatcher, runtime = _dispatcher()
  child = dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {}, type='bro')

  def defer_then_crash(context, peer, message):
    context.query(peer, message)
    raise RuntimeError('after defer')

  dispatcher.on('work', defer_then_crash)

  dispatcher.on_message('requester', _request('work', {'id': 'child', 'wait': 1}, 'work'))

  assert 'after defer' in caplog.text
  assert runtime.sent == []
  dispatcher.journal.end(child, {'outcome': 'ok', 'value': 'answer'})
  await _settle()
  assert [
    message.payload['value']['mission']['result']['value'] for _, message in runtime.sent
  ] == ['answer']


def test_a_primitive_failing_before_supervision_is_denied_without_a_record(caplog):
  dispatcher, runtime = _dispatcher()
  dispatcher.on(
    'work',
    lambda context, peer, message: context.job(
      CommandJob(('true',), {}), peer, type='benchmark', timeout=None
    ),
  )

  dispatcher.on_message('requester', _request('work', {}, 'work'))

  assert 'no job output' in caplog.text
  assert runtime.sent[-1][1].outcome == 'denied'
  assert not dispatcher.journal.knows('work')
  assert 'work' not in dispatcher.live


@pytest.mark.asyncio
async def test_a_supervisor_failing_to_begin_ends_its_record_with_one_launch_failure(caplog):
  def refuse(seconds, callback):
    raise OSError('no timers')

  dispatcher, runtime = _dispatcher(schedule=refuse)
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), type='bro', timeout=None
    ),
  )

  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()

  assert 'no timers' in caplog.text
  assert [message.type for _, message in runtime.sent] == ['result']
  result = runtime.sent[0][1]
  assert result.outcome == 'failed'
  assert result.payload['detail']['reason'] == 'launch'
  assert dispatcher.journal.records['work'].state == 'ended'
  assert dispatcher.journal.records['work'].settled is True
  assert 'work' not in dispatcher.live
  assert runtime.handle is None


def test_handler_deny_answers_and_journals_refused_work():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(
    'work', lambda context, peer, message: context.deny(peer, 'not allowed', type='bro')
  )
  dispatcher.on_message('requester', _request('work', {'target': 'x'}, 'denied'))
  assert runtime.sent[-1][1].payload == {'outcome': 'denied', 'error': 'not allowed'}
  assert dispatcher.journal.records['denied'].state == 'denied'
  assert dispatcher.journal.records['denied'].type == 'bro'
  assert dispatcher.journal.records['denied'].settled is True

  dispatcher.on(QUERY, query_handler)
  dispatcher.on_message(
    'requester',
    _request(QUERY, {'id': 'denied', 'wait': 10, 'settled': True}, 'denied-query'),
  )
  assert runtime.sent[-1][1].payload['value']['mission']['settled'] is True


def test_handler_deny_bounds_the_response_to_a_maximum_frame_request():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(
    'work',
    lambda context, peer, message: context.deny(peer, f'unknown bro {message.args["target"]}'),
  )

  dispatcher.on_message('requester', _maximum_frame_request())

  response = runtime.sent[-1][1]
  assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  assert response.outcome == 'denied'
  assert response.payload['detail']['truncated'] is True


@pytest.mark.asyncio
async def test_job_output_open_failure_closes_the_journal_record():
  class RaisingOutput:
    def open(self):
      raise OSError('cannot open')

    async def collect(self, directory, context, requester):
      raise AssertionError('collect called after open failed')

  dispatcher, runtime = _dispatcher(job_output=RaisingOutput())
  dispatcher.on(
    'job',
    lambda context, peer, message: context.job(
      CommandJob(('true',), {}), peer, type='benchmark', timeout=None
    ),
  )
  dispatcher.on_message('requester', _request('job', {}, 'job'))
  await _settle()
  record = dispatcher.journal.records['job']
  assert record.state == 'ended'
  assert record.reason == 'output'
  assert record.talk == frozenset()
  assert record.type == 'benchmark'
  assert runtime.sent[-1][1].payload['detail']['reason'] == 'output'


@pytest.mark.asyncio
async def test_spawn_launch_failure_synthesizes_one_terminal():
  dispatcher, runtime = _dispatcher()
  runtime.launch_error = RuntimeError('launch broke')
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), type='bro', timeout=None
    ),
  )
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  results = [message for _, message in runtime.sent if message.type == 'result']
  assert len(results) == 1
  assert results[0].payload['detail']['reason'] == 'launch'
  assert dispatcher.journal.records['work'].state == 'ended'
  assert dispatcher.journal.records['work'].type == 'bro'


@pytest.mark.asyncio
async def test_expected_ready_failure_synthesizes_one_terminal():
  dispatcher, runtime = _dispatcher()

  def fail_ready(provisioned):
    raise OSError('ready broke')

  dispatcher.on(
    'manual',
    lambda context, peer, message: context.expect(
      peer, talk=frozenset(), ready=fail_ready, type='bro'
    ),
  )
  dispatcher.on_message('requester', _request('manual', {}, 'manual'))
  await _settle()
  results = [message for _, message in runtime.sent if message.type == 'result']
  assert len(results) == 1
  assert results[0].payload['detail']['reason'] == 'launch'
  assert dispatcher.journal.records['manual'].state == 'ended'
  assert dispatcher.journal.records['manual'].type == 'bro'


@pytest.mark.asyncio
async def test_messages_sent_during_launch_follow_started_in_the_journal():
  dispatcher, runtime = _dispatcher()
  runtime.launch_messages = [
    brotocol.mark('work', 'trail', trail_id='trail'),
    brotocol.result('work', 'ok'),
  ]
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), type='bro', timeout=None
    ),
  )
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  transitions = [
    event['transition']
    for event in dispatcher.journal.events_after(0, 'requester', dispatcher.workers)[1]
    if event['mission'] == 'work'
  ]
  assert transitions == ['accepted', 'started', 'trail', 'ended']
  assert runtime.handle is not None
  runtime.handle.exit.set_result(0)
  await _settle()


@pytest.mark.asyncio
async def test_spawned_quest_marks_lifecycle_and_routes_only_its_worker():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), type='bro', timeout=None
    ),
  )
  dispatcher.on_message('requester', _request('work', {'prompt': 'go'}, 'work'))
  assert runtime.sent[-1][1].payload == {'transition': 'accepted'}
  await _settle()
  worker = dispatcher.journal.records['work'].worker
  assert worker is not None
  assert runtime.sent[-1][1].payload == {'transition': 'started'}
  delivered = len(runtime.sent)
  runtime.events[worker].on_message(brotocol.result('forged', 'ok'))
  assert len(runtime.sent) == delivered
  assert 'work' in dispatcher.live
  dispatcher.on_message('impostor', brotocol.mark('work', 'trail', trail_id='wrong'))
  dispatcher.on_message(worker, brotocol.mark('work', 'trail', trail_id='trail-1'))
  dispatcher.on_message(worker, brotocol.result('work', 'ok', value='done'))
  assert dispatcher.journal.records['work'].trail_id == 'trail-1'
  assert dispatcher.journal.records['work'].result == {'outcome': 'ok', 'value': 'done'}
  assert dispatcher.journal.records['work'].settled is False
  assert 'work' not in dispatcher.live
  assert runtime.handle is not None
  runtime.handle.exit.set_result(7)
  await _settle()
  assert dispatcher.journal.records['work'].outcome == 'ok'
  assert dispatcher.journal.records['work'].settled is True
  assert [message.type for _, message in runtime.sent] == ['mark', 'mark', 'mark', 'result']


@pytest.mark.asyncio
async def test_result_disarms_the_deadline_while_the_worker_stays_routable():
  schedule = FakeScheduler()
  dispatcher, runtime = _dispatcher(schedule=schedule)
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), timeout=10, type='bro'
    ),
  )
  dispatcher.on(PING, ping_handler)
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  worker = dispatcher.journal.records['work'].worker
  assert worker is not None
  quest_deadline = schedule.timers[-1]
  assert not quest_deadline.cancelled
  runtime.events[worker].on_message(brotocol.result('work', 'ok'))
  assert quest_deadline.cancelled
  assert runtime.handle is not None
  runtime.events[worker].on_message(_request(PING, {'nested': True}, 'nested'))
  assert runtime.sent[-1][1].payload['value'] == {'nested': True}
  runtime.handle.exit.set_result(0)
  await _settle()


@pytest.mark.asyncio
async def test_query_can_wait_for_worker_supervision_to_settle_after_its_result():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  dispatcher.on('work', _spawn_handler(cast(Spawner, runtime)))
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  worker = dispatcher.journal.records['work'].worker
  assert worker is not None
  runtime.events[worker].on_message(brotocol.result('work', 'ok'))
  delivered = len(runtime.sent)

  dispatcher.on_message(
    'requester',
    _request(QUERY, {'id': 'work', 'wait': 10, 'settled': True}, 'settlement-query'),
  )
  await _settle()
  assert len(runtime.sent) == delivered
  assert runtime.handle is not None
  runtime.handle.exit.set_result(0)
  await _settle()

  _, answer = runtime.sent[-1]
  assert answer.request_id == 'settlement-query'
  assert answer.payload['value']['mission']['settled'] is True


@pytest.mark.asyncio
async def test_none_timeout_leaves_the_started_mission_unbounded():
  schedule = FakeScheduler()
  dispatcher, runtime = _dispatcher(schedule=schedule)
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(),
      cast(Spawner, runtime),
      peer,
      talk=frozenset(),
      timeout=None,
      type='bro',
    ),
  )

  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()

  assert [(timer.seconds, timer.cancelled) for timer in schedule.timers] == [(LAUNCH_TIMEOUT, True)]
  assert 'work' in dispatcher.live
  assert runtime.handle is not None
  runtime.handle.exit.set_result(0)
  await _settle()


@pytest.mark.asyncio
async def test_process_cannot_emit_dispatcher_or_worker_marks():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), type='bro', timeout=None
    ),
  )
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  worker = dispatcher.journal.records['work'].worker
  assert worker is not None
  dispatcher.on_message(worker, brotocol.mark('work', 'accepted'))
  dispatcher.on_message(worker, brotocol.mark('work', 'started'))
  transitions = [
    event['transition']
    for event in dispatcher.journal.events_after(0, 'requester', dispatcher.workers)[1]
    if event['mission'] == 'work'
  ]
  assert transitions == ['accepted', 'started']
  assert runtime.handle is not None
  runtime.handle.exit.set_result(0)
  await _settle()


@pytest.mark.asyncio
async def test_expected_worker_defers_wire_acceptance_until_ready_and_starts_on_attach():
  dispatcher, runtime = _dispatcher()
  ready = []
  dispatcher.on(
    'manual',
    lambda context, peer, message: context.expect(
      peer, talk=frozenset(), ready=ready.append, type='bro'
    ),
  )
  dispatcher.on_message('requester', _request('manual', {}, 'manual'))
  assert runtime.sent == []
  await _settle()
  assert len(ready) == 1
  assert runtime.sent[-1][1].payload == {'transition': 'accepted'}
  worker = dispatcher.journal.records['manual'].worker
  runtime.events[worker].on_connect()
  assert runtime.sent[-1][1].payload == {'transition': 'started'}
  runtime.events[worker].on_disconnect()
  await _settle()
  assert dispatcher.journal.records['manual'].reason == 'disconnected'


def _live_chat(dispatcher, *, talk):
  record = dispatcher.journal.open(
    'child', 'summon', 'root-quest', 'requester', {}, talk=frozenset(talk), type='bro'
  )
  dispatcher.journal.bind(record, 'child-worker')
  dispatcher.live[record.mission_id] = record
  dispatcher.workers['child-worker'] = record.mission_id
  return record


def test_message_routes_by_its_named_quest_in_both_directions():
  dispatcher, runtime = _dispatcher()
  record = _live_chat(dispatcher, talk={'owner.say', 'worker.say'})

  requester_message = brotocol.message(record.mission_id, {'text': 'start'})
  worker_message = brotocol.message(record.mission_id, {'text': 'working'})
  dispatcher.on_message('requester', requester_message)
  dispatcher.on_message('child-worker', worker_message)

  assert runtime.sent == [
    ('child-worker', requester_message),
    ('requester', worker_message),
  ]
  assert [entry['from'] for entry in record.messages] == ['owner', 'worker']


def test_message_from_a_stranger_is_dropped_without_journaling(caplog):
  dispatcher, runtime = _dispatcher()
  record = _live_chat(dispatcher, talk={'worker.say'})
  before = dispatcher.journal.head

  dispatcher.on_message('stranger', brotocol.message(record.mission_id, {'text': 'forged'}))

  assert runtime.sent == []
  assert dispatcher.journal.head == before
  assert 'not an end of the mission' in caplog.text


def test_disallowed_message_is_journaled_as_a_correlated_refusal(caplog):
  dispatcher, runtime = _dispatcher()
  record = _live_chat(dispatcher, talk={'worker.say'})
  candidate = brotocol.message(
    record.mission_id,
    {'text': 'may I?'},
    id='Q1',
    reply_to='Q0',
  )

  dispatcher.on_message('child-worker', candidate)

  assert runtime.sent == []
  assert record.messages[-1]['reason'] == 'worker lacks the talk right for this message'
  assert record.messages[-1]['id'] == 'Q1'
  assert record.messages[-1]['reply_to'] == 'Q0'
  assert 'lacks the talk right' in caplog.text


def test_a_message_at_the_bound_is_routed_whole():
  dispatcher, runtime = _dispatcher()
  record = _live_chat(dispatcher, talk={'worker.say'})
  message = brotocol.message(record.mission_id, {'text': text_at_the_message_bound()})

  dispatcher.on_message('child-worker', message)

  assert runtime.sent == [('requester', message)]
  assert record.messages[-1]['head'] == message.payload


def test_oversized_message_is_journaled_as_a_correlated_refusal(caplog):
  dispatcher, runtime = _dispatcher()
  record = _live_chat(dispatcher, talk={'worker.say', 'worker.question'})
  candidate = brotocol.message(
    record.mission_id, {'text': text_at_the_message_bound() + 'x'}, id='Q1'
  )

  dispatcher.on_message('child-worker', candidate)

  assert runtime.sent == []
  assert record.pending == []
  assert record.messages[-1]['transition'] == 'refused'
  assert record.messages[-1]['id'] == 'Q1'
  assert f'{MAX_MESSAGE_BYTES + 1} bytes' in record.messages[-1]['reason']
  assert record.messages[-1]['head']['truncated'] is True
  assert 'exceeds' in caplog.text


def test_listening_is_worker_born_set_once_and_repeats_are_silent():
  dispatcher, runtime = _dispatcher()
  record = _live_chat(dispatcher, talk={'worker.say'})
  listening = brotocol.mark(record.mission_id, 'listening')

  dispatcher.on_message('requester', listening)
  assert not record.listening
  dispatcher.on_message('child-worker', listening)
  dispatcher.on_message('child-worker', listening)

  assert record.listening
  assert runtime.sent == [('requester', listening)]
  transitions = [
    event['transition']
    for event in dispatcher.journal.events_after(0, 'requester', dispatcher.workers)[1]
    if event['mission'] == record.mission_id
  ]
  assert transitions.count('listening') == 1


def test_message_on_a_non_live_mission_is_dropped(caplog):
  dispatcher, runtime = _dispatcher()
  dispatcher.on_message('requester', brotocol.message('ended', {'text': 'late'}))
  assert runtime.sent == []
  assert 'no live mission' in caplog.text


def test_spawn_and_expect_require_explicit_talk():
  dispatcher, runtime = _dispatcher()
  spawner = cast(Spawner, runtime)
  with pytest.raises(TypeError, match='talk'):
    dispatcher.spawn(  # type: ignore[call-arg]
      LaunchSpec(), spawner, 'requester', type='bro', timeout=None
    )
  with pytest.raises(TypeError, match='talk'):
    dispatcher.expect(  # type: ignore[call-arg]
      'requester', ready=lambda provisioned: None, type='bro'
    )


def test_spawn_and_job_require_an_explicit_timeout():
  dispatcher, runtime = _dispatcher()
  with pytest.raises(TypeError, match='timeout'):
    dispatcher.spawn(  # type: ignore[call-arg]
      LaunchSpec(), cast(Spawner, runtime), 'requester', type='bro', talk=frozenset()
    )
  with pytest.raises(TypeError, match='timeout'):
    dispatcher.job(  # type: ignore[call-arg]
      CommandJob(('true',), {}), 'requester', type='benchmark'
    )


def test_query_lists_the_callers_children_and_reads_its_own_quest_by_id():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = dispatcher.journal.open(
    'child', 'summon', 'root-quest', 'requester', {'target': 'dev'}, type='bro'
  )
  dispatcher.journal.end(child, {'outcome': 'ok', 'value': 'answer'})
  dispatcher.on_message('requester', _request(QUERY, {}, 'list'))
  listed = runtime.sent[-1][1].payload['value']['missions']
  assert {record['id'] for record in listed} == {'child'}
  dispatcher.on_message('requester', _request(QUERY, {'id': 'child'}, 'query-one'))
  assert runtime.sent[-1][1].payload['value']['mission']['result']['value'] == 'answer'
  dispatcher.on_message('requester', _request(QUERY, {'id': 'root-quest'}, 'query-self'))
  assert runtime.sent[-1][1].payload['value']['mission']['id'] == 'root-quest'
  assert not dispatcher.journal.knows('list')
  assert not dispatcher.journal.knows('query-one')
  assert not dispatcher.journal.knows('query-self')


def test_query_returns_the_minimal_view_of_an_evicted_child():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {}, type='bro')
  dispatcher.journal.end(child, {'outcome': 'ok'})
  dispatcher.journal.records.pop(child.mission_id)

  dispatcher.on_message('requester', _request(QUERY, {'id': child.mission_id}, 'query-evicted'))

  response = runtime.sent[-1][1]
  assert response.payload['value']['mission'] == {
    'id': child.mission_id,
    'kind': 'summon',
    'parent': 'root-quest',
    'state': 'evicted',
  }


def test_query_reports_a_retained_result_as_evicted_when_its_response_would_exceed_the_frame():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {}, type='bro')
  dispatcher.journal.end(child, _maximum_frame_result_payload())

  dispatcher.on_message('requester', _request(QUERY, {'id': 'child'}, 'query-one'))

  response = runtime.sent[-1][1]
  assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  view = response.payload['value']['mission']
  assert 'result' not in view
  assert view['result_evicted'] is True


def test_query_trims_the_oldest_chat_tail_entries_but_keeps_every_pending_question():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = _live_chat(dispatcher, talk={'worker.say', 'worker.question'})
  padding = 'x' * (brotocol.MAX_IDENTIFIER_BYTES - 3)
  for index in range(MAX_PENDING_QUESTIONS):
    dispatcher.journal.message(
      child,
      'worker',
      brotocol.message(
        child.mission_id, {'text': text_at_the_message_bound()}, id=f'Q{index:02d}' + padding
      ),
    )
  for _ in range(MAX_RECORD_MESSAGES - MAX_PENDING_QUESTIONS):
    dispatcher.journal.message(
      child,
      'worker',
      brotocol.message(
        child.mission_id, {'text': text_at_the_message_bound()}, reply_to='R' + padding
      ),
    )
  assert len(child.pending) == MAX_PENDING_QUESTIONS
  assert len(child.messages) == MAX_RECORD_MESSAGES

  dispatcher.on_message('requester', _request(QUERY, {'id': child.mission_id}, 'query-chat'))

  response = runtime.sent[-1][1]
  view = response.payload['value']['mission']
  assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  assert view['messages_truncated'] is True
  assert 'pending' not in view
  marked = [entry for entry in view['messages'] if entry.get('pending') is True]
  unmarked = [entry for entry in view['messages'] if entry.get('pending') is not True]
  assert [entry['id'] for entry in marked] == [entry['id'] for entry in child.pending]
  assert 0 < len(unmarked) < MAX_RECORD_MESSAGES - MAX_PENDING_QUESTIONS
  # the oldest says went first; the ones kept are the newest suffix of the says
  says = [entry for entry in child.messages if entry.get('id') is None]
  assert unmarked == says[-len(unmarked) :]


def test_the_largest_pending_set_fits_both_query_projections():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  # a legal identifier whose JSON encoding is as large as the bound allows
  expanding = '\x00' * (brotocol.MAX_IDENTIFIER_BYTES // 6)
  child = dispatcher.journal.open(
    expanding,
    'summon',
    'root-quest',
    'requester',
    {f'field-{index}': 'x' * ARGS_STRING_HEAD for index in range(ARGS_HEAD_BYTES)},
    talk=frozenset({'owner.question', 'worker.say', 'worker.question'}),
    type='bro',
  )
  dispatcher.journal.bind(child, 'child-worker')
  dispatcher.live[child.mission_id] = child
  dispatcher.workers['child-worker'] = child.mission_id
  dispatcher.journal.trail(child, expanding)
  for index in range(MAX_PENDING_QUESTIONS):
    dispatcher.journal.message(
      child,
      'worker',
      brotocol.message(
        child.mission_id,
        {'text': text_at_the_message_bound()},
        id=f'{index:04d}' + '\x00' * ((brotocol.MAX_IDENTIFIER_BYTES - 4) // 6),
        reply_to=expanding,
      ),
    )
  for _ in range(MAX_RECORD_MESSAGES - MAX_PENDING_QUESTIONS):
    dispatcher.journal.message(
      child, 'worker', brotocol.message(child.mission_id, {'text': text_at_the_message_bound()})
    )
  assert len(child.pending) == MAX_PENDING_QUESTIONS
  request = '\x01' * (brotocol.MAX_IDENTIFIER_BYTES // 6)

  dispatcher.on_message('requester', _request(QUERY, {'id': child.mission_id}, request))
  by_id = runtime.sent[-1][1]
  dispatcher.on_message('requester', _request(QUERY, {}, request))
  listing = runtime.sent[-1][1]

  assert len(by_id.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  marked = [
    entry for entry in by_id.payload['value']['mission']['messages'] if entry.get('pending')
  ]
  assert [entry['id'] for entry in marked] == [entry['id'] for entry in child.pending]
  assert len(listing.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  [record] = listing.payload['value']['missions']
  assert record['pending'] == child.pending


def test_query_trims_chat_before_evicting_a_retained_result():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = _live_chat(dispatcher, talk={'worker.say'})
  for _ in range(MAX_RECORD_MESSAGES):
    dispatcher.journal.message(
      child,
      'worker',
      brotocol.message(child.mission_id, {'text': text_at_the_message_bound()}),
    )
  dispatcher.journal.end(
    child, {'outcome': 'ok', 'value': 'answer' * (brotocol.MAX_FRAME_BYTES // 10)}
  )

  dispatcher.on_message('requester', _request(QUERY, {'id': child.mission_id}, 'query-chat'))

  response = runtime.sent[-1][1]
  view = response.payload['value']['mission']
  assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  assert view['messages_truncated'] is True
  assert view['result'] == child.result
  assert 'result_evicted' not in view
  assert view['messages'] == child.messages[-len(view['messages']) :]


def test_query_pages_every_live_record_inside_the_frame_cap():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  for index in range(256):
    dispatcher.journal.open(
      f'child-{index}',
      'summon',
      'root-quest',
      'requester',
      {f'field-{field}': str(index) * 200 for field in range(20)},
      type='bro',
    )
  cursor = None
  seen = []
  page = 0
  while True:
    args = {} if cursor is None else {'cursor': cursor}
    dispatcher.on_message('requester', _request(QUERY, args, f'list-{page}'))
    response = runtime.sent[-1][1]
    assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
    value = response.payload['value']
    seen.extend(record['id'] for record in value['missions'])
    cursor = value.get('cursor')
    if cursor is None:
      break
    page += 1
  assert set(seen) == set(dispatcher.journal.records) - {'root-quest'}
  assert len(seen) == len(set(seen))


def test_query_rejects_an_invalid_listing_cursor():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  dispatcher.on_message('requester', _request(QUERY, {'cursor': 'not-a-cursor'}, 'list'))
  assert runtime.sent[-1][1].outcome == 'denied'


@pytest.mark.asyncio
async def test_query_wait_answers_the_terminal_state():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {}, type='bro')
  dispatcher.on_message('requester', _request(QUERY, {'id': 'child', 'wait': 1}, 'wait'))
  assert runtime.sent == []
  dispatcher.journal.end(child, {'outcome': 'ok', 'value': 'answer'})
  await _settle()
  assert runtime.sent[-1][1].payload['value']['mission']['result']['value'] == 'answer'


@pytest.mark.asyncio
async def test_query_since_returns_when_the_quests_chat_sequence_advances():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = _live_chat(dispatcher, talk={'worker.say'})
  dispatcher.on_message(
    'requester',
    _request(QUERY, {'id': child.mission_id, 'since': child.chat_seq, 'wait': 1}, 'wait-chat'),
  )
  assert runtime.sent == []

  dispatcher.on_message('child-worker', brotocol.message(child.mission_id, {'text': 'working'}))
  await _settle()

  response = runtime.sent[-1][1].payload['value']['mission']
  assert response['chat_seq'] == child.chat_seq
  assert response['messages'][-1]['head'] == {'text': 'working'}


@pytest.mark.asyncio
async def test_query_wait_reports_an_oversize_retained_result_as_evicted():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {}, type='bro')
  dispatcher.on_message('requester', _request(QUERY, {'id': 'child', 'wait': 1}, 'wait'))

  dispatcher.journal.end(child, _maximum_frame_result_payload())
  await _settle()

  response = runtime.sent[-1][1]
  assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  view = response.payload['value']['mission']
  assert 'result' not in view
  assert view['result_evicted'] is True


def test_events_from_now_and_retained_history():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(EVENTS, events_handler)
  dispatcher.on_message('requester', _request(EVENTS, {}, 'now'))
  assert runtime.sent[-1][1].payload['value'] == {
    'head': dispatcher.journal.head,
    'events': [],
  }
  dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {}, type='bro')
  dispatcher.on_message('requester', _request(EVENTS, {'after': 0}, 'history'))
  history = runtime.sent[-1][1].payload['value']['events']
  assert [event['mission'] for event in history] == ['child']


def test_events_pages_every_visible_event_inside_the_frame_cap():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(EVENTS, events_handler)
  for index in range(130):
    record = dispatcher.journal.open(
      f'child-{index}',
      'summon',
      'root-quest',
      'requester',
      {},
      type='bro',
    )
    dispatcher.journal.trail(record, 'x' * brotocol.MAX_IDENTIFIER_BYTES)

  cursor = 0
  seen = []
  page_sizes = []
  while cursor < dispatcher.journal.head:
    dispatcher.on_message('requester', _request(EVENTS, {'after': cursor}, f'events-{cursor}'))
    response = runtime.sent[-1][1]
    assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
    events = response.payload['value']['events']
    seen.extend(event['seq'] for event in events)
    page_sizes.append(len(events))
    cursor = events[-1]['seq']

  assert seen == list(range(2, dispatcher.journal.head + 1))
  assert page_sizes[0] < 256


@pytest.mark.asyncio
async def test_events_wait_answers_when_a_visible_event_arrives():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(EVENTS, events_handler)
  head = dispatcher.journal.head
  dispatcher.on_message('requester', _request(EVENTS, {'after': head, 'wait': 1}, 'wait-events'))
  assert runtime.sent == []
  for index in range(130):
    record = dispatcher.journal.open(
      f'child-{index}',
      'summon',
      'root-quest',
      'requester',
      {},
      type='bro',
    )
    dispatcher.journal.trail(record, 'x' * brotocol.MAX_IDENTIFIER_BYTES)
  await _settle()

  response = runtime.sent[-1][1]
  assert len(response.to_bytes()) <= brotocol.MAX_FRAME_BYTES
  events = response.payload['value']['events']
  assert events[0]['mission'] == 'child-0'
  assert len(events) < 256


@pytest.mark.asyncio
async def test_worker_death_synthesizes_one_failed_result():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(
      LaunchSpec(), cast(Spawner, runtime), peer, talk=frozenset(), type='bro', timeout=None
    ),
  )
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  assert runtime.handle is not None
  runtime.handle.exit.set_result(7)
  await _settle()
  results = [message for _, message in runtime.sent if message.type == 'result']
  assert len(results) == 1
  assert results[0].payload['detail']['reason'] == 'exit'
  assert dispatcher.journal.records['work'].outcome == 'failed'


@pytest.mark.asyncio
async def test_sigterm_ends_an_owning_run_through_its_teardown():
  runtime = FakeRuntime()
  dispatcher = Dispatcher()
  dispatcher.bind(cast(Runtime, runtime))
  run = asyncio.create_task(
    dispatcher.run(LaunchSpec(), cast(Spawner, runtime), end_on_sigterm=True, type='bro')
  )
  await _settle()
  assert runtime.handle is not None
  assert not runtime.handle.killed

  os.kill(os.getpid(), signal.SIGTERM)

  assert await run == TERMINATED_EXIT_CODE
  assert runtime.handle.killed
  assert runtime.stopped
  (root,) = [record for record in dispatcher.journal.records.values() if record.kind == 'root']
  assert root.state == 'ended'
  assert root.reason == 'killed'
  assert root.talk == frozenset()


@pytest.mark.asyncio
async def test_an_owning_run_holds_sigterm_through_its_teardown():
  runtime = FakeRuntime()
  runtime.kill_release = asyncio.Event()
  dispatcher = Dispatcher()
  dispatcher.bind(cast(Runtime, runtime))
  run = asyncio.create_task(
    dispatcher.run(LaunchSpec(), cast(Spawner, runtime), end_on_sigterm=True, type='bro')
  )
  await _settle()
  assert runtime.handle is not None

  os.kill(os.getpid(), signal.SIGTERM)
  await _settle()
  assert runtime.handle.killed
  assert not run.done()
  os.kill(os.getpid(), signal.SIGTERM)
  await _settle()
  assert not run.done()

  runtime.kill_release.set()
  assert await run == TERMINATED_EXIT_CODE
  assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_a_run_leaves_the_processs_sigterm_alone_unless_asked():
  outcome: dict[str, object] = {}

  def drive() -> None:
    async def run() -> int:
      runtime = FakeRuntime()
      dispatcher = Dispatcher()
      dispatcher.bind(cast(Runtime, runtime))
      task = asyncio.create_task(dispatcher.run(LaunchSpec(), cast(Spawner, runtime), type='bro'))
      await _settle()
      assert runtime.handle is not None
      runtime.handle.exit.set_result(7)
      return await task

    outcome['code'] = asyncio.run(run())

  before = signal.getsignal(signal.SIGTERM)
  thread = threading.Thread(target=drive)
  thread.start()
  thread.join()

  assert outcome['code'] == 7
  assert signal.getsignal(signal.SIGTERM) is before


@pytest.mark.asyncio
async def test_a_dead_requester_orphans_the_quests_it_asked_for_down_the_tree():
  dispatcher, runtime = _dispatcher()
  dispatcher.on('work', _spawn_handler(cast(Spawner, runtime)))
  dispatcher.on_message('requester', _request('work', {}, 'child'))
  await _settle()
  child_peer = dispatcher.journal.records['child'].worker
  assert child_peer is not None
  runtime.events[child_peer].on_message(_request('work', {}, 'grandchild'))
  await _settle()
  grandchild_peer = dispatcher.journal.records['grandchild'].worker
  assert grandchild_peer is not None
  runtime.events[grandchild_peer].on_message(_request('work', {}, 'great-grandchild'))
  await _settle()
  delivered_before = len(runtime.sent)

  runtime.handles['child'].exit.set_result(1)
  await _settle()
  await _settle()

  records = dispatcher.journal.records
  assert (records['child'].outcome, records['child'].reason) == ('failed', 'exit')
  assert (records['grandchild'].outcome, records['grandchild'].reason) == ('failed', 'orphaned')
  assert records['great-grandchild'].reason == 'orphaned'
  assert records['grandchild'].result == {
    'outcome': 'failed',
    'detail': {'reason': 'orphaned', 'exit_code': -15, 'output_tail': 'output'},
  }
  assert runtime.handles['grandchild'].killed
  assert runtime.handles['great-grandchild'].killed
  assert dispatcher.live == {}
  assert set(dispatcher.workers) == {'requester'}
  # the dead child's own failure reaches its requester; nothing is sent to the dead child
  assert [
    (peer, message.request_id, message.payload['detail']['reason'])
    for peer, message in runtime.sent[delivered_before:]
  ] == [('requester', 'child', 'exit')]


@pytest.mark.asyncio
async def test_root_exit_keeps_closing_live_quests_as_killed():
  runtime = FakeRuntime()
  dispatcher = Dispatcher()
  dispatcher.bind(cast(Runtime, runtime))
  dispatcher.on('work', _spawn_handler(cast(Spawner, runtime)))
  run = asyncio.create_task(dispatcher.run(LaunchSpec(), cast(Spawner, runtime), type='bro'))
  await _settle()
  root_peer = dispatcher.root
  assert root_peer is not None
  runtime.events[root_peer].on_message(_request('work', {}, 'child'))
  await _settle()

  runtime.handles[dispatcher.workers[root_peer]].exit.set_result(0)

  assert await run == 0
  child = dispatcher.journal.records['child']
  assert (child.outcome, child.reason) == ('killed', 'killed')
  assert runtime.handles['child'].killed


@pytest.mark.asyncio
async def test_cancel_ends_the_requesters_live_quest_and_orphans_what_it_asked_for():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(CANCEL, cancel_handler)
  dispatcher.on('work', _spawn_handler(cast(Spawner, runtime)))
  dispatcher.on_message('requester', _request('work', {}, 'child'))
  await _settle()
  child_peer = dispatcher.journal.records['child'].worker
  assert child_peer is not None
  runtime.events[child_peer].on_message(_request('work', {}, 'grandchild'))
  await _settle()

  dispatcher.on_message('requester', _request(CANCEL, {'id': 'child'}, 'cancel-child'))

  reply = runtime.sent[-1][1]
  assert (reply.request_id, reply.payload) == ('cancel-child', {'outcome': 'ok'})
  assert not dispatcher.journal.knows('cancel-child')
  await _settle()
  await _settle()
  assert runtime.handles['child'].killed
  child = dispatcher.journal.records['child']
  assert (child.outcome, child.reason) == ('failed', 'cancelled')
  assert child.result == {
    'outcome': 'failed',
    'detail': {'reason': 'cancelled', 'exit_code': -15, 'output_tail': 'output'},
  }
  assert [
    peer
    for peer, message in runtime.sent
    if message.type == Tag.RESULT and message.request_id == 'child'
  ] == ['requester']
  assert dispatcher.journal.records['grandchild'].reason == 'orphaned'
  assert runtime.handles['grandchild'].killed


@pytest.mark.asyncio
async def test_cancel_during_launch_answers_at_once_and_ends_the_quest_on_the_late_reap():
  dispatcher, runtime = _dispatcher()
  runtime.launch_gate = asyncio.Event()
  dispatcher.on(CANCEL, cancel_handler)
  dispatcher.on('work', _spawn_handler(cast(Spawner, runtime)))
  dispatcher.on_message('requester', _request('work', {}, 'child'))
  await _settle()
  assert dispatcher.journal.records['child'].started_at is None

  dispatcher.on_message('requester', _request(CANCEL, {'id': 'child'}, 'cancel-child'))
  await _settle()

  assert [(message.request_id, message.outcome) for _, message in runtime.sent[-1:]] == [
    ('cancel-child', 'ok'),
  ]
  assert 'child' in dispatcher.live
  runtime.launch_gate.set()
  await _settle()
  await _settle()
  assert runtime.handles['child'].killed
  child = dispatcher.journal.records['child']
  assert (child.outcome, child.reason) == ('failed', 'cancelled')
  assert [(message.request_id, message.outcome) for _, message in runtime.sent[-1:]] == [
    ('child', 'failed'),
  ]


@pytest.mark.asyncio
async def test_a_result_sent_during_the_kill_does_not_outrun_the_cancel():
  dispatcher, runtime = _dispatcher()
  runtime.kill_release = asyncio.Event()
  dispatcher.on(CANCEL, cancel_handler)
  dispatcher.on('work', _spawn_handler(cast(Spawner, runtime)))
  dispatcher.on_message('requester', _request('work', {}, 'child'))
  await _settle()
  child_peer = dispatcher.journal.records['child'].worker
  assert child_peer is not None

  dispatcher.on_message('requester', _request(CANCEL, {'id': 'child'}, 'cancel-child'))
  await _settle()
  runtime.events[child_peer].on_message(brotocol.result('child', 'ok', value='too late'))
  await _settle()

  assert 'child' in dispatcher.live
  assert runtime.handles['child'].killed
  assert not runtime.handles['child'].exit.done()
  runtime.kill_release.set()
  await _settle()
  await _settle()
  child = dispatcher.journal.records['child']
  assert (child.outcome, child.reason) == ('failed', 'cancelled')
  assert [
    message.outcome
    for _, message in runtime.sent
    if message.type == Tag.RESULT and message.request_id == 'child'
  ] == ['failed']


def test_cancel_is_denied_unless_the_peer_requested_a_live_quest():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(CANCEL, cancel_handler)
  _live_chat(dispatcher, talk=set())
  ended = dispatcher.journal.open('done', 'summon', 'root-quest', 'requester', {}, type='bro')
  dispatcher.journal.end(ended, {'outcome': 'ok'})

  dispatcher.on_message('child-worker', _request(CANCEL, {'id': 'child'}, 'cancel-1'))
  dispatcher.on_message('stranger', _request(CANCEL, {'id': 'child'}, 'cancel-2'))
  dispatcher.on_message('requester', _request(CANCEL, {'id': 'missing'}, 'cancel-3'))
  dispatcher.on_message('requester', _request(CANCEL, {'id': 'done'}, 'cancel-4'))
  dispatcher.on_message('requester', _request(CANCEL, {}, 'cancel-5'))
  dispatcher.on_message('requester', _request(CANCEL, {'id': 'child', 'now': True}, 'cancel-6'))

  assert [message.outcome for _, message in runtime.sent] == ['denied'] * 6
  assert 'child' in dispatcher.live
  assert not any(dispatcher.journal.knows(f'cancel-{index}') for index in range(1, 7))
