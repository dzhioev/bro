import asyncio
from pathlib import Path
from typing import Optional, cast

import pytest

from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.dispatcher import (
  EVENTS,
  PING,
  QUERY,
  Dispatcher,
  events_handler,
  ping_handler,
  query_handler,
)
from bro.broker.job import CommandJob
from bro.broker.runtime import Runtime
from bro.broker.spawn import LaunchSpec
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint


class FakeHandle:
  def __init__(self):
    self.exit = asyncio.get_running_loop().create_future()
    self.killed = False

  async def wait(self):
    return await self.exit

  async def kill(self):
    self.killed = True
    if not self.exit.done():
      self.exit.set_result(-15)

  def output_tail(self):
    return 'output'


class FakeRuntime:
  def __init__(self):
    self.sent = []
    self.events = {}
    self.handle = None
    self.stopped = False
    self.launch_messages = []
    self.launch_error: Optional[Exception] = None
    self.provision_error: Optional[Exception] = None

  async def provision(self, events):
    if self.provision_error is not None:
      raise self.provision_error
    channel = f'worker-{len(self.events) + 1}'
    self.events[channel] = events
    return Provisioned(channel, Endpoint(1234, f'token-{channel}'))

  async def launch(self, launch, provisioned, quest):
    if self.launch_error is not None:
      raise self.launch_error
    self.handle = FakeHandle()
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


def _dispatcher(*, job_output=None):
  runtime = FakeRuntime()
  dispatcher = Dispatcher(job_output=job_output)
  dispatcher.bind(cast(Runtime, runtime))
  root = dispatcher.journal.open('root-quest', 'root', None, None, {})
  dispatcher.journal.bind(root, 'requester')
  dispatcher.workers['requester'] = 'root-quest'
  return dispatcher, runtime


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


def test_handler_deny_answers_and_journals_refused_work():
  dispatcher, runtime = _dispatcher()
  dispatcher.on('work', lambda context, peer, message: context.deny(peer, 'not allowed'))
  dispatcher.on_message('requester', _request('work', {'target': 'x'}, 'denied'))
  assert runtime.sent[-1][1].payload == {'outcome': 'denied', 'error': 'not allowed'}
  assert dispatcher.journal.records['denied'].state == 'denied'


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
    lambda context, peer, message: context.job(CommandJob(('true',), {}), peer),
  )
  dispatcher.on_message('requester', _request('job', {}, 'job'))
  await _settle()
  record = dispatcher.journal.records['job']
  assert record.state == 'ended'
  assert record.reason == 'output'
  assert runtime.sent[-1][1].payload['detail']['reason'] == 'output'


@pytest.mark.asyncio
async def test_spawn_launch_failure_synthesizes_one_terminal():
  dispatcher, runtime = _dispatcher()
  runtime.launch_error = RuntimeError('launch broke')
  dispatcher.on('work', lambda context, peer, message: context.spawn(LaunchSpec(), peer))
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  results = [message for _, message in runtime.sent if message.type == 'result']
  assert len(results) == 1
  assert results[0].payload['detail']['reason'] == 'launch'
  assert dispatcher.journal.records['work'].state == 'ended'


@pytest.mark.asyncio
async def test_expected_ready_failure_synthesizes_one_terminal():
  dispatcher, runtime = _dispatcher()

  def fail_ready(provisioned):
    raise OSError('ready broke')

  dispatcher.on(
    'manual',
    lambda context, peer, message: context.expect(peer, timeout=None, ready=fail_ready),
  )
  dispatcher.on_message('requester', _request('manual', {}, 'manual'))
  await _settle()
  results = [message for _, message in runtime.sent if message.type == 'result']
  assert len(results) == 1
  assert results[0].payload['detail']['reason'] == 'launch'
  assert dispatcher.journal.records['manual'].state == 'ended'


@pytest.mark.asyncio
async def test_messages_sent_during_launch_follow_started_in_the_journal():
  dispatcher, runtime = _dispatcher()
  runtime.launch_messages = [
    brotocol.mark('work', 'trail', trail_id='trail'),
    brotocol.result('work', 'ok'),
  ]
  dispatcher.on('work', lambda context, peer, message: context.spawn(LaunchSpec(), peer))
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  transitions = [
    event['transition']
    for event in dispatcher.journal.events_after(0, 'requester', dispatcher.workers)[1]
    if event['quest'] == 'work'
  ]
  assert transitions == ['accepted', 'started', 'trail', 'ended']
  assert runtime.handle is not None
  runtime.handle.exit.set_result(0)
  await _settle()


@pytest.mark.asyncio
async def test_spawned_quest_marks_lifecycle_and_routes_only_its_worker():
  dispatcher, runtime = _dispatcher()
  dispatcher.on('work', lambda context, peer, message: context.spawn(LaunchSpec(), peer))
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
  assert 'work' not in dispatcher.live
  assert runtime.handle is not None
  runtime.handle.exit.set_result(0)
  await _settle()
  assert [message.type for _, message in runtime.sent] == ['mark', 'mark', 'mark', 'result']


@pytest.mark.asyncio
async def test_result_disarms_the_deadline_while_the_worker_stays_routable():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(
    'work',
    lambda context, peer, message: context.spawn(LaunchSpec(), peer, timeout=0.01),
  )
  dispatcher.on(PING, ping_handler)
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  worker = dispatcher.journal.records['work'].worker
  assert worker is not None
  runtime.events[worker].on_message(brotocol.result('work', 'ok'))
  await asyncio.sleep(0.03)
  assert runtime.handle is not None
  assert not runtime.handle.killed
  runtime.events[worker].on_message(_request(PING, {'nested': True}, 'nested'))
  assert runtime.sent[-1][1].payload['value'] == {'nested': True}
  runtime.handle.exit.set_result(0)
  await _settle()


@pytest.mark.asyncio
async def test_process_cannot_emit_dispatcher_or_worker_marks():
  dispatcher, runtime = _dispatcher()
  dispatcher.on('work', lambda context, peer, message: context.spawn(LaunchSpec(), peer))
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  worker = dispatcher.journal.records['work'].worker
  assert worker is not None
  dispatcher.on_message(worker, brotocol.mark('work', 'accepted'))
  dispatcher.on_message(worker, brotocol.mark('work', 'started'))
  transitions = [
    event['transition']
    for event in dispatcher.journal.events_after(0, 'requester', dispatcher.workers)[1]
    if event['quest'] == 'work'
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
    lambda context, peer, message: context.expect(peer, timeout=None, ready=ready.append),
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


def test_query_lists_and_reads_only_the_callers_subtree():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(QUERY, query_handler)
  child = dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {'target': 'dev'})
  dispatcher.journal.end(child, {'outcome': 'ok', 'value': 'answer'})
  dispatcher.on_message('requester', _request(QUERY, {}, 'list'))
  listed = runtime.sent[-1][1].payload['value']['quests']
  assert {record['id'] for record in listed} == {'root-quest', 'child'}
  dispatcher.on_message('requester', _request(QUERY, {'id': 'child'}, 'query-one'))
  assert runtime.sent[-1][1].payload['value']['quest']['result']['value'] == 'answer'
  assert not dispatcher.journal.knows('list')
  assert not dispatcher.journal.knows('query-one')


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
    seen.extend(record['id'] for record in value['quests'])
    cursor = value.get('cursor')
    if cursor is None:
      break
    page += 1
  assert set(seen) == set(dispatcher.journal.records)
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
  child = dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {})
  dispatcher.on_message('requester', _request(QUERY, {'id': 'child', 'wait': 1}, 'wait'))
  assert runtime.sent == []
  dispatcher.journal.end(child, {'outcome': 'ok', 'value': 'answer'})
  await _settle()
  assert runtime.sent[-1][1].payload['value']['quest']['result']['value'] == 'answer'


def test_events_from_now_and_retained_history():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(EVENTS, events_handler)
  dispatcher.on_message('requester', _request(EVENTS, {}, 'now'))
  assert runtime.sent[-1][1].payload['value'] == {
    'head': dispatcher.journal.head,
    'events': [],
  }
  dispatcher.on_message('requester', _request(EVENTS, {'after': 0}, 'history'))
  history = runtime.sent[-1][1].payload['value']['events']
  assert history[0]['quest'] == 'root-quest'


@pytest.mark.asyncio
async def test_events_wait_answers_when_a_visible_event_arrives():
  dispatcher, runtime = _dispatcher()
  dispatcher.on(EVENTS, events_handler)
  head = dispatcher.journal.head
  dispatcher.on_message('requester', _request(EVENTS, {'after': head, 'wait': 1}, 'wait-events'))
  assert runtime.sent == []
  dispatcher.journal.open('child', 'summon', 'root-quest', 'requester', {})
  await _settle()
  events = runtime.sent[-1][1].payload['value']['events']
  assert events[0]['quest'] == 'child'


@pytest.mark.asyncio
async def test_worker_death_synthesizes_one_failed_result():
  dispatcher, runtime = _dispatcher()
  dispatcher.on('work', lambda context, peer, message: context.spawn(LaunchSpec(), peer))
  dispatcher.on_message('requester', _request('work', {}, 'work'))
  await _settle()
  assert runtime.handle is not None
  runtime.handle.exit.set_result(7)
  await _settle()
  results = [message for _, message in runtime.sent if message.type == 'result']
  assert len(results) == 1
  assert results[0].payload['detail']['reason'] == 'exit'
  assert dispatcher.journal.records['work'].outcome == 'failed'
