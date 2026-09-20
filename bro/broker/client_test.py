import asyncio
import contextlib
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

import pytest

from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.client import (
  BROKER_CHANNEL,
  BROKER_MISSION,
  BROKER_UPSTREAM,
  Client,
  ReplyDeadline,
  talk_from_env,
)
from bro.broker.environment import BROKER_TALK
from bro.broker.transport import ChannelID, ClientTransport
from bro.broker.transports.tcp import LOCAL_HOST, TcpClientTransport, TcpServerTransport

TIMEOUT = 5.0


class StubSink:
  """records inbound traffic onto asyncio queues the test coroutine can await."""

  def __init__(self):
    self.connects: asyncio.Queue = asyncio.Queue()  # channel
    self.messages: asyncio.Queue = asyncio.Queue()  # (channel, message)
    self.disconnects: asyncio.Queue = asyncio.Queue()  # channel

  async def on_connect(self, channel: ChannelID) -> None:
    self.connects.put_nowait(channel)

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    self.messages.put_nowait((channel, message))

  async def on_disconnect(self, channel: ChannelID) -> None:
    self.disconnects.put_nowait(channel)


@dataclass
class Harness:
  transport: TcpServerTransport
  sink: StubSink


@contextlib.asynccontextmanager
async def running_server():
  transport = TcpServerTransport([LOCAL_HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _transport(provisioned) -> TcpClientTransport:
  """the attach handshake blocks on the server's ack, so a client is built off the
  loop thread that has to answer it."""
  return await asyncio.to_thread(TcpClientTransport, provisioned.host_endpoint.address(LOCAL_HOST))


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


def test_from_env_returns_none_when_no_channel_is_intended(monkeypatch):
  monkeypatch.delenv(BROKER_CHANNEL, raising=False)
  monkeypatch.delenv(BROKER_UPSTREAM, raising=False)
  assert Client.from_env() is None


def test_from_env_reports_a_failed_session_proxy(monkeypatch):
  monkeypatch.delenv(BROKER_CHANNEL, raising=False)
  monkeypatch.setenv(BROKER_UPSTREAM, 'tcp://upstream@127.0.0.1:7')
  with pytest.raises(RuntimeError, match='session proxy failed at launch'):
    Client.from_env()


@pytest.mark.asyncio
async def test_from_env_connects_and_sends(monkeypatch):
  async with running_server() as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(BROKER_CHANNEL, provisioned.host_endpoint.address(LOCAL_HOST))
    client = await asyncio.to_thread(Client.from_env)
    assert client is not None

    await asyncio.to_thread(client.send, 'ping', {'n': 1})
    channel, message = await _next(server.sink.messages)
    assert channel == provisioned.channel
    assert message.type == 'request'
    assert message.payload == {'kind': 'ping', 'args': {'n': 1}}
    assert message.request is None
    client.close()


@pytest.mark.asyncio
async def test_mark_and_result_emit_against_a_quest():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    await asyncio.to_thread(client.mark, 'X', 'trail', trail_id='t1')
    await asyncio.to_thread(client.result, 'X', {'outcome': 'ok', 'value': 'answer'})

    _, trail = await _next(server.sink.messages)
    _, done = await _next(server.sink.messages)
    assert (trail.type, trail.request, trail.payload) == (
      'mark',
      'X',
      {'transition': 'trail', 'trail_id': 't1'},
    )
    assert (done.type, done.request) == ('result', 'X')
    assert done.payload == {'outcome': 'ok', 'value': 'answer'}
    client.close()


@pytest.mark.asyncio
async def test_request_correlates_and_sets_unrelated_aside():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {'n': 1}, TIMEOUT))

    channel, request_message = await _next(server.sink.messages)
    assert request_message.kind == 'ping'
    unrelated = brotocol.message('some-other-quest', {'note': 'unrelated'})
    await server.transport.send(channel, unrelated)
    await server.transport.send(
      channel, brotocol.result(request_message.id, 'ok', value={'pong': 1})
    )

    reply = await asyncio.wait_for(request_task, TIMEOUT)
    assert reply.type == 'result'
    assert reply.request == request_message.id
    assert reply.payload == {'outcome': 'ok', 'value': {'pong': 1}}

    # the unrelated message request() read past was set aside, not dropped
    set_aside = await asyncio.to_thread(client.receive, 0.2)
    assert set_aside == unrelated
    client.close()


@pytest.mark.asyncio
async def test_request_times_out_without_reply():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    with pytest.raises(TimeoutError):
      await asyncio.to_thread(client.request, 'ping', {}, 0.2)
    client.close()


@pytest.mark.asyncio
async def test_request_raises_on_channel_close():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    await _next(server.sink.messages)  # the request reached the host

    await server.transport.close(provisioned.channel)
    with pytest.raises(ConnectionError):
      await asyncio.wait_for(request_task, TIMEOUT)
    client.close()


@pytest.mark.asyncio
async def test_call_surfaces_marks_and_message_then_returns_the_result():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    interims: list[Message] = []
    call_task = asyncio.create_task(
      asyncio.to_thread(
        client.call, 'summon', {'target': 'dev'}, TIMEOUT, on_interim=interims.append
      )
    )

    channel, request_message = await _next(server.sink.messages)
    assert request_message.kind == 'summon'
    await server.transport.send(channel, brotocol.mark(request_message.id, 'accepted'))
    await server.transport.send(channel, brotocol.message(request_message.id, {'note': 'working'}))
    unrelated = brotocol.message('some-other-quest', {'note': 'unrelated'})
    await server.transport.send(channel, unrelated)
    await server.transport.send(channel, brotocol.result(request_message.id, 'ok', value='r'))

    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.type == 'result'
    assert result.payload == {'outcome': 'ok', 'value': 'r'}
    assert [(interim.type, interim.payload) for interim in interims] == [
      ('mark', {'transition': 'accepted'}),
      ('message', {'note': 'working'}),
    ]

    # the uncorrelated message call() read past was set aside, not dropped
    set_aside = await asyncio.to_thread(client.receive, 0.2)
    assert set_aside == unrelated
    client.close()


@pytest.mark.asyncio
async def test_call_rides_every_message_while_await_any_returns_the_first():
  # every correlated message rides through a call's wait; await_any is the one
  # surface that returns the first correlated message as-is — what a manual
  # summon's acceptance handshake reads
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    interims: list[Message] = []
    call_task = asyncio.create_task(
      asyncio.to_thread(client.call, 'summon', {}, TIMEOUT, on_interim=interims.append)
    )
    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.message(request_message.id, {}))
    await server.transport.send(channel, brotocol.message(request_message.id, {'trail_id': 't1'}))
    await server.transport.send(channel, brotocol.result(request_message.id, 'ok', value='r'))
    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.type == 'result'
    assert [interim.payload for interim in interims] == [{}, {'trail_id': 't1'}]

    sent = client.send('summon', {})
    any_task = asyncio.create_task(asyncio.to_thread(client.await_any, sent, TIMEOUT))
    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.mark(request_message.id, 'accepted'))
    first = await asyncio.wait_for(any_task, TIMEOUT)
    assert (first.type, first.request) == ('mark', sent.id)
    client.close()


@pytest.mark.asyncio
async def test_call_without_callback_skips_message_and_returns_failed():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    call_task = asyncio.create_task(asyncio.to_thread(client.call, 'summon', {}, TIMEOUT))

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.message(request_message.id, {'trail_id': 't'}))
    await server.transport.send(
      channel, brotocol.result(request_message.id, 'failed', detail={'reason': 'exit'})
    )

    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.type == 'result'
    assert result.payload == {'outcome': 'failed', 'detail': {'reason': 'exit'}}
    client.close()


@pytest.mark.asyncio
async def test_call_deadline_spans_interim_message():
  # `timeout` bounds the whole call: an interim message does not extend the result wait.
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    call_task = asyncio.create_task(asyncio.to_thread(client.call, 'summon', {}, 0.3))

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.message(request_message.id, {}))
    with pytest.raises(TimeoutError):
      await asyncio.wait_for(call_task, TIMEOUT)
    client.close()


@pytest.mark.asyncio
async def test_send_returns_the_sent_request():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    sent = await asyncio.to_thread(client.send, 'summon', {'target': 'dev'})

    _, received = await _next(server.sink.messages)
    # the id is minted client-side, so the caller holds it before any reply exists
    assert received.id == sent.id
    assert received.kind == 'summon'
    client.close()


@pytest.mark.asyncio
async def test_await_reply_reattaches_to_a_sent_request():
  # send + await_reply is call() split in two: the id is exposed between them
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    sent = await asyncio.to_thread(client.send, 'summon', {'target': 'dev'})
    interims: list[Message] = []
    await_task = asyncio.create_task(
      asyncio.to_thread(client.await_reply, sent, TIMEOUT, on_interim=interims.append)
    )

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.message(request_message.id, {'trail_id': 't1'}))
    await server.transport.send(channel, brotocol.result(request_message.id, 'ok', value='r'))

    result = await asyncio.wait_for(await_task, TIMEOUT)
    assert result.type == 'result'
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
    client.close()


def test_reply_deadline_rearms_on_an_interim():
  deadline = ReplyDeadline(1.0, 100.0, after_interim=10.0)
  assert deadline.remaining(100.5) == 0.5

  deadline.observe(brotocol.message('quest', {}), 100.25)

  assert deadline.remaining(100.5) == 9.75
  assert deadline.timeout == 10.0


def test_reply_deadline_rearms_only_on_the_interims_it_is_told_to():
  deadline = ReplyDeadline(
    1.0,
    100.0,
    after_interim=10.0,
    rearms_on=lambda message: message.payload.get('transition') == 'trail',
  )

  deadline.observe(brotocol.mark('quest', 'accepted'), 100.5)
  assert deadline.remaining(100.5) == 0.5
  deadline.observe(brotocol.mark('quest', 'trail', trail_id='t1'), 100.5)
  assert deadline.remaining(100.5) == 10.0


def test_reply_deadline_rearm_shortens_a_longer_bound():
  deadline = ReplyDeadline(20.0, 100.0, after_interim=0.25)

  deadline.observe(brotocol.message('quest', {}), 100.5)

  assert deadline.remaining(100.5) == 0.25
  assert deadline.timeout == 0.25
  assert not deadline.expired(100.5)
  assert deadline.expired(100.75)


def test_reply_deadline_is_unbounded_without_a_timeout_until_an_interim():
  deadline = ReplyDeadline(None, 100.0, after_interim=5.0)
  assert deadline.remaining(1e9) is None
  assert not deadline.expired(1e9)

  deadline.observe(brotocol.message('quest', {}), 100.5)

  assert deadline.remaining(100.5) == 5.0


def test_await_reply_bounds_the_read_by_the_rearmed_deadline():
  request = brotocol.request('summon', {})
  transport = FakeClientTransport(
    [brotocol.message(request.request_id, {}), brotocol.result(request.request_id, 'ok')]
  )
  client = Client(transport)

  result = client.await_reply(request, 20.0, timeout_after_interim=0.25)

  assert result.outcome == 'ok'
  first, second = transport.read_bounds
  assert first is not None and first > 0.25
  assert second is not None and second <= 0.25


@pytest.mark.asyncio
async def test_close_confirm_returns_after_the_host_consumed_everything():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    await asyncio.to_thread(client.result, 'X', {'outcome': 'ok', 'value': 'r'})
    await asyncio.wait_for(asyncio.to_thread(client.close, True), TIMEOUT)
    # the host closes back only after its read loop consumed the frame, so the
    # message must already be here — no await
    assert server.sink.messages.qsize() == 1


@pytest.mark.asyncio
async def test_close_aborts_a_blocked_wait_from_another_thread():
  # the cross-thread abort guarantee (ClientTransport.close): a controller that
  # abandoned an off-thread wait closes the client, and the blocked receive
  # returns as channel EOF instead of hanging until traffic arrives
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    await _next(server.sink.messages)  # the request reached the host; the wait is blocked

    await asyncio.to_thread(client.close)
    with pytest.raises(ConnectionError):
      await asyncio.wait_for(request_task, TIMEOUT)


@pytest.mark.asyncio
async def test_receive_returns_none_on_timeout():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    assert await asyncio.to_thread(client.receive, 0.2) is None
    client.close()


@pytest.mark.asyncio
async def test_await_reply_recovers_a_result_set_aside_during_a_journal_query():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    original = await asyncio.to_thread(client.send, 'summon', {})
    original_id = original.id
    assert original_id is not None
    query_task = asyncio.create_task(
      asyncio.to_thread(client.call, 'query', {'id': original_id}, TIMEOUT)
    )

    channel, _ = await _next(server.sink.messages)
    _, query = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.result(original_id, 'ok', value='answer'))
    await server.transport.send(
      channel,
      brotocol.result(query.id or '', 'ok', value={'quest': {'id': original_id, 'state': 'ended'}}),
    )
    await query_task

    result = await asyncio.to_thread(client.await_reply, original, TIMEOUT)
    assert result.payload == {'outcome': 'ok', 'value': 'answer'}
    client.close()


@pytest.mark.asyncio
async def test_message_mints_question_id_and_carries_reply_correlation():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))

    sent = await asyncio.to_thread(
      client.message,
      'quest',
      {'text': 'answer?'},
      reply_to='earlier',
      question=True,
    )
    _, received = await _next(server.sink.messages)

    assert received == sent
    assert sent.type == Tag.MESSAGE
    assert sent.id is not None
    assert sent.reply_to == 'earlier'
    client.close()


@pytest.mark.asyncio
async def test_listen_emits_the_worker_listening_mark():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))

    await asyncio.to_thread(client.listen, 'quest')
    _, received = await _next(server.sink.messages)

    assert received == brotocol.mark('quest', 'listening')
    client.close()


@pytest.mark.asyncio
async def test_await_reply_to_correlates_on_message_id_and_sets_other_traffic_aside():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    question = await asyncio.to_thread(client.message, 'quest', {}, question=True)
    await _next(server.sink.messages)
    wait = asyncio.create_task(asyncio.to_thread(client.await_reply_to, question, TIMEOUT))

    unrelated = brotocol.message('quest', {'text': 'other'}, reply_to='other-question')
    reply = brotocol.message('quest', {'text': 'answer'}, reply_to=question.id)
    await server.transport.send(provisioned.channel, unrelated)
    await server.transport.send(provisioned.channel, reply)

    assert await asyncio.wait_for(wait, TIMEOUT) == reply
    assert await asyncio.to_thread(client.receive, 0.2) == unrelated
    client.close()


@pytest.mark.asyncio
async def test_await_reply_to_recovers_a_reply_set_aside_during_a_journal_query():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    question = await asyncio.to_thread(client.message, 'quest', {}, question=True)
    assert question.id is not None
    await _next(server.sink.messages)
    query_task = asyncio.create_task(
      asyncio.to_thread(client.call, 'query', {'id': 'quest'}, TIMEOUT)
    )
    channel, query = await _next(server.sink.messages)

    reply = brotocol.message('quest', {'text': 'answer'}, reply_to=question.id)
    await server.transport.send(channel, reply)
    await server.transport.send(
      channel,
      brotocol.result(query.id or '', 'ok', value={'quest': {'id': 'quest'}}),
    )
    await query_task

    assert await asyncio.to_thread(client.await_reply_to, question, TIMEOUT) == reply
    client.close()


@pytest.mark.asyncio
async def test_await_reply_until_returns_the_matching_interim():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    request = await asyncio.to_thread(client.send, 'summon', {})
    await _next(server.sink.messages)
    wait = asyncio.create_task(
      asyncio.to_thread(
        client.await_reply,
        request,
        TIMEOUT,
        until=lambda message: message.type == Tag.MESSAGE and message.is_question,
      )
    )

    question = brotocol.message(request.request_id, {'text': 'approve?'}, id='question')
    await server.transport.send(provisioned.channel, question)

    assert await asyncio.wait_for(wait, TIMEOUT) == question
    client.close()


def test_talk_from_env_distinguishes_unpublished_and_empty_talk(monkeypatch):
  monkeypatch.delenv(BROKER_TALK, raising=False)
  assert talk_from_env() is None
  monkeypatch.setenv(BROKER_TALK, '')
  assert talk_from_env() == frozenset()
  monkeypatch.setenv(BROKER_TALK, 'worker.question,worker.say')
  assert talk_from_env() == frozenset({'worker.say', 'worker.question'})


def test_talk_from_env_rejects_an_unknown_right(monkeypatch):
  monkeypatch.setenv(BROKER_TALK, 'worker.guess')
  with pytest.raises(ValueError, match='worker.guess'):
    talk_from_env()


@pytest.mark.parametrize(
  ('talk', 'reply_to', 'question', 'missing_right'),
  [
    ('worker.question', None, False, 'worker.say'),
    ('worker.say', None, True, 'worker.question'),
    ('worker.say', 'owner-question', False, 'owner.question'),
  ],
)
def test_message_refuses_a_move_missing_from_the_own_quest_talk(
  monkeypatch, talk, reply_to, question, missing_right
):
  transport = FakeClientTransport()
  client = Client(transport)
  monkeypatch.setenv(BROKER_MISSION, 'own-quest')
  monkeypatch.setenv(BROKER_TALK, talk)

  with pytest.raises(PermissionError, match=missing_right):
    client.message('own-quest', {}, reply_to=reply_to, question=question)

  assert transport.sent == []


def test_message_does_not_apply_the_own_talk_to_a_child_quest(monkeypatch):
  transport = FakeClientTransport()
  client = Client(transport)
  monkeypatch.setenv(BROKER_MISSION, 'own-quest')
  monkeypatch.setenv(BROKER_TALK, '')

  sent = client.message('child-quest', {})

  assert transport.sent == [sent]


class FakeClientTransport(ClientTransport):
  def __init__(self, inbound: Sequence[Message] = ()):
    self.sent: list[Message] = []
    self.inbound = deque(inbound)
    self.read_bounds: list[Optional[float]] = []

  def send(self, message: Message) -> None:
    self.sent.append(message)

  def receive(self, timeout):
    self.read_bounds.append(timeout)
    if len(self.inbound) == 0:
      raise AssertionError('receive called past the scripted traffic')
    return self.inbound.popleft()

  def close(self, confirm=False):
    pass
