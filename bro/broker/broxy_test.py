import asyncio
import contextlib
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import bro.broker.broxy as broker_broxy
from bro.broker.brotocol import Message, Tag
from bro.broker.broxy import Broxy
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.transport import ChannelID
from bro.broker.transports.unix import UnixClientTransport, UnixServerTransport

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
  transport: UnixServerTransport
  sink: StubSink
  channel: ChannelID
  socket_path: Path
  broxy: Broxy
  run_task: asyncio.Task


@contextlib.asynccontextmanager
async def running_broxy(**broxy_kwargs):
  """an upstream broker transport (with a stub sink) plus a Broxy proxying one
  provisioned channel onto a local socket, both live on the test's loop.

  sockets live in a short /tmp mkdtemp dir, not pytest's tmp_path: the lulid-named
  channel socket must fit sun_path (~104 bytes on macOS), which the deep per-test
  dirs — and even the resolved system temp dir — exceed."""
  socket_dir = Path(tempfile.mkdtemp(prefix='broxy-', dir='/tmp'))
  transport = UnixServerTransport(str(socket_dir / 'upstream'))
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  provisioned = await transport.provision()
  socket_path = socket_dir / 'broxy.sock'
  broxy = Broxy('unix:' + str(provisioned.host_endpoint), socket_path, **broxy_kwargs)
  run_task = asyncio.create_task(broxy.run())
  assert await asyncio.to_thread(broker_broxy._await_ready, str(socket_path), TIMEOUT) == 0
  try:
    yield Harness(
      transport=transport,
      sink=sink,
      channel=provisioned.channel,
      socket_path=socket_path,
      broxy=broxy,
      run_task=run_task,
    )
  finally:
    broxy.stop()
    await asyncio.wait_for(run_task, TIMEOUT)
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)
    shutil.rmtree(socket_dir, ignore_errors=True)


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


async def _wait_until(condition, message: str):
  deadline = asyncio.get_running_loop().time() + TIMEOUT
  while not bool(condition()):
    if asyncio.get_running_loop().time() > deadline:
      raise AssertionError(message)
    await asyncio.sleep(0.01)


def _local_client(harness: Harness) -> Client:
  return Client(UnixClientTransport(str(harness.socket_path)))


async def _detached_request(harness: Harness, type: str) -> Message:
  """send a request through the broxy and close the connection with the delivery
  handshake, so by return the broxy has detached the waiter — messages for the
  request id deterministically buffer from here on."""
  transport = UnixClientTransport(str(harness.socket_path))
  request = Message(type=type, payload={})
  await asyncio.to_thread(transport.send, request)
  await asyncio.wait_for(asyncio.to_thread(transport.close, True), TIMEOUT)
  _, seen = await _next(harness.sink.messages)
  assert seen.id == request.id
  return request


@pytest.mark.asyncio
async def test_request_round_trips_through_the_broxy():
  async with running_broxy() as harness:
    client = _local_client(harness)
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {'n': 1}, TIMEOUT))

    channel, message = await _next(harness.sink.messages)
    assert channel == harness.channel  # arrived upstream on the session's one channel
    assert message.type == 'ping'
    assert message.payload == {'n': 1}
    await harness.transport.send(
      channel, Message(type='reply', payload={'pong': 1}, in_reply_to=message.id)
    )

    reply = await asyncio.wait_for(request_task, TIMEOUT)
    assert reply.type == 'reply'
    assert reply.in_reply_to == message.id
    assert reply.payload == {'pong': 1}
    client.close()


@pytest.mark.asyncio
async def test_from_env_client_works_through_the_broxy(monkeypatch):
  async with running_broxy() as harness:
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + str(harness.socket_path))
    client = Client.from_env()
    assert client is not None
    await asyncio.to_thread(client.send, 'started', {'trail_id': 't1'})
    channel, message = await _next(harness.sink.messages)
    assert channel == harness.channel
    assert message.type == 'started'
    assert message.payload == {'trail_id': 't1'}
    client.close()


@pytest.mark.asyncio
async def test_call_rides_interim_started_through_the_broxy():
  async with running_broxy() as harness:
    client = _local_client(harness)
    interims: list[Message] = []
    call_task = asyncio.create_task(
      asyncio.to_thread(client.call, 'summon', {}, TIMEOUT, on_interim=interims.append)
    )

    channel, request = await _next(harness.sink.messages)
    await harness.transport.send(
      channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await harness.transport.send(
      channel,
      Message(
        type='completed', payload={'result': 'ok', 'end_reason': 'ok'}, in_reply_to=request.id
      ),
    )

    terminal = await asyncio.wait_for(call_task, TIMEOUT)
    assert terminal.type == 'completed'
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
    client.close()


@pytest.mark.asyncio
async def test_accepted_is_interim_and_leaves_the_conversation_pending():
  # an accepted delivered and read must not spend the conversation: a manual
  # summon's client detaches right after it, and the token stays checkable
  async with running_broxy() as harness:
    client = _local_client(harness)
    request = client.send('summon', {'manual': True})
    channel, seen = await _next(harness.sink.messages)
    assert seen.id == request.id
    await harness.transport.send(channel, Message(type='accepted', payload={}, in_reply_to=request.id))  # fmt: skip
    first = await asyncio.to_thread(client.await_any, request, TIMEOUT)
    assert first.type == 'accepted'
    client.close()

    checker = _local_client(harness)
    reply = await asyncio.to_thread(checker.call, 'check', {'id': request.id}, TIMEOUT)
    assert reply.payload['state'] == 'pending'
    checker.close()


@pytest.mark.asyncio
async def test_concurrent_connections_route_stickily():
  async with running_broxy() as harness:
    client_a = _local_client(harness)
    client_b = _local_client(harness)
    task_a = asyncio.create_task(
      asyncio.to_thread(client_a.request, 'ping', {'from': 'a'}, TIMEOUT)
    )
    request_a = (await _next(harness.sink.messages))[1]
    task_b = asyncio.create_task(
      asyncio.to_thread(client_b.request, 'ping', {'from': 'b'}, TIMEOUT)
    )
    request_b = (await _next(harness.sink.messages))[1]

    # replies interleaved out of request order still land on their own connections
    await harness.transport.send(
      harness.channel, Message(type='reply', payload={'to': 'b'}, in_reply_to=request_b.id)
    )
    await harness.transport.send(
      harness.channel, Message(type='reply', payload={'to': 'a'}, in_reply_to=request_a.id)
    )
    assert (await asyncio.wait_for(task_b, TIMEOUT)).payload == {'to': 'b'}
    assert (await asyncio.wait_for(task_a, TIMEOUT)).payload == {'to': 'a'}
    client_a.close()
    client_b.close()


@pytest.mark.asyncio
async def test_claim_replays_buffered_messages_in_order():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')
    await harness.transport.send(
      harness.channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await harness.transport.send(
      harness.channel,
      Message(
        type='completed', payload={'result': 'ok', 'end_reason': 'ok'}, in_reply_to=request.id
      ),
    )
    await _wait_until(
      lambda: harness.broxy._routes[request.id].terminal_seq is not None,
      'the terminal never reached the mailbox',
    )

    claimer = _local_client(harness)
    interims: list[Message] = []
    terminal = await asyncio.to_thread(
      claimer.call, 'claim', {'id': request.id}, TIMEOUT, on_interim=interims.append
    )
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
    assert terminal.type == 'completed'
    assert terminal.payload == {'result': 'ok', 'end_reason': 'ok'}
    assert terminal.in_reply_to != request.id  # re-tagged to correlate to the claim
    claimer.close()

    # the replay read the conversation through its terminal: the collect path is
    # spent, and a second claim fails fast pointing at the cursor re-read
    late = _local_client(harness)
    reply = await asyncio.to_thread(late.request, 'claim', {'id': request.id}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert request.id in reply.payload['error']
    assert 'already collected' in reply.payload['error']
    late.close()


@pytest.mark.asyncio
async def test_claim_unknown_id_replies_error_immediately():
  async with running_broxy() as harness:
    client = _local_client(harness)
    reply = await asyncio.to_thread(client.request, 'claim', {'id': 'never-minted'}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert 'never-minted' in reply.payload['error']
    client.close()

    # the claim was handled locally: the only message upstream ever sees is this ping
    probe = _local_client(harness)
    probe_task = asyncio.create_task(asyncio.to_thread(probe.request, 'ping', {}, TIMEOUT))
    channel, message = await _next(harness.sink.messages)
    assert message.type == 'ping'
    await harness.transport.send(channel, Message(type='reply', payload={}, in_reply_to=message.id))
    await asyncio.wait_for(probe_task, TIMEOUT)
    assert harness.sink.messages.qsize() == 0
    probe.close()


@pytest.mark.asyncio
async def test_claim_after_live_terminal_replies_error():
  async with running_broxy() as harness:
    client = _local_client(harness)
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    _, request = await _next(harness.sink.messages)
    await harness.transport.send(
      harness.channel, Message(type='reply', payload={}, in_reply_to=request.id)
    )
    await asyncio.wait_for(request_task, TIMEOUT)

    reply = await asyncio.to_thread(client.request, 'claim', {'id': request.id}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert request.id in reply.payload['error']
    assert 'already collected' in reply.payload['error']
    client.close()


@pytest.mark.asyncio
async def test_claim_re_awaits_a_pending_request():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')

    claimer = _local_client(harness)
    claim_task = asyncio.create_task(
      asyncio.to_thread(claimer.request, 'claim', {'id': request.id}, TIMEOUT)
    )
    await _wait_until(
      lambda: harness.broxy._routes[request.id].waiter is not None,
      'the claim never took the route over',
    )
    await harness.transport.send(
      harness.channel,
      Message(type='completed', payload={'result': 'late'}, in_reply_to=request.id),
    )
    terminal = await asyncio.wait_for(claim_task, TIMEOUT)
    assert terminal.type == 'completed'
    assert terminal.payload == {'result': 'late'}
    claimer.close()


@pytest.mark.asyncio
async def test_claim_with_a_live_waiter_fails_fast():
  # the wait is a lock: the original waiter keeps its route; the newcomer errors
  async with running_broxy() as harness:
    original = UnixClientTransport(str(harness.socket_path))
    request = Message(type='summon', payload={})
    await asyncio.to_thread(original.send, request)
    await _next(harness.sink.messages)

    claimer = _local_client(harness)
    reply = await asyncio.to_thread(claimer.request, 'claim', {'id': request.id}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert 'already being awaited' in reply.payload['error']
    claimer.close()

    # the original waiter's route survived: the terminal still reaches it
    await harness.transport.send(
      harness.channel, Message(type='completed', payload={'result': 'mine'}, in_reply_to=request.id)
    )
    terminal = await asyncio.to_thread(original.receive, TIMEOUT)
    assert terminal is not None
    assert terminal.payload == {'result': 'mine'}
    original.close()


@pytest.mark.asyncio
async def test_second_claim_while_first_is_live_fails_fast():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')

    first = _local_client(harness)
    first_task = asyncio.create_task(
      asyncio.to_thread(first.request, 'claim', {'id': request.id}, TIMEOUT)
    )
    await _wait_until(
      lambda: harness.broxy._routes[request.id].waiter is not None,
      'the first claim never took the route over',
    )

    second = _local_client(harness)
    reply = await asyncio.to_thread(second.request, 'claim', {'id': request.id}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert 'already being awaited' in reply.payload['error']
    second.close()

    # the first claim still collects
    await harness.transport.send(
      harness.channel,
      Message(type='completed', payload={'result': 'first'}, in_reply_to=request.id),
    )
    terminal = await asyncio.wait_for(first_task, TIMEOUT)
    assert terminal.payload == {'result': 'first'}
    first.close()


@pytest.mark.asyncio
async def test_check_unknown_id_replies_state_unknown():
  async with running_broxy() as harness:
    client = _local_client(harness)
    reply = await asyncio.to_thread(client.request, 'check', {'id': 'never-minted'}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert reply.payload == {'state': 'unknown'}
    client.close()


@pytest.mark.asyncio
async def test_check_pending_reports_state_and_buffered_trail_id():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')
    checker = _local_client(harness)

    # nothing retained yet: pending without a trail id
    reply = await asyncio.to_thread(checker.request, 'check', {'id': request.id}, TIMEOUT)
    assert reply.payload == {'state': 'pending', 'seq': 0}

    await harness.transport.send(
      harness.channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await _wait_until(
      lambda: len(harness.broxy._routes[request.id].messages) > 0,
      'the started interim never reached the mailbox',
    )
    reply = await asyncio.to_thread(checker.request, 'check', {'id': request.id}, TIMEOUT)
    assert reply.payload == {'state': 'pending', 'seq': 1, 'trail_id': 't1'}
    checker.close()


@pytest.mark.asyncio
async def test_check_replays_a_buffered_terminal_without_consuming():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')
    await harness.transport.send(
      harness.channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await harness.transport.send(
      harness.channel,
      Message(
        type='completed', payload={'result': 'ok', 'end_reason': 'ok'}, in_reply_to=request.id
      ),
    )
    await _wait_until(
      lambda: harness.broxy._routes[request.id].terminal_seq is not None,
      'the terminal never reached the mailbox',
    )

    # two checks in a row both see the full replay — the peek consumes nothing
    for _ in range(2):
      checker = _local_client(harness)
      interims: list[Message] = []
      terminal = await asyncio.to_thread(
        checker.call, 'check', {'id': request.id}, TIMEOUT, on_interim=interims.append
      )
      assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
      assert terminal.type == 'completed'
      assert terminal.payload == {'result': 'ok', 'end_reason': 'ok'}
      assert terminal.in_reply_to != request.id  # re-tagged to correlate to the check
      checker.close()

    # the entry is still claimable for real afterwards
    claimer = _local_client(harness)
    terminal = await asyncio.to_thread(claimer.call, 'claim', {'id': request.id}, TIMEOUT)
    assert terminal.payload == {'result': 'ok', 'end_reason': 'ok'}
    claimer.close()


@pytest.mark.asyncio
async def test_check_does_not_disturb_a_live_waiter():
  async with running_broxy() as harness:
    waiter = _local_client(harness)
    waiter_task = asyncio.create_task(asyncio.to_thread(waiter.request, 'summon', {}, TIMEOUT))
    _, request = await _next(harness.sink.messages)

    checker = _local_client(harness)
    reply = await asyncio.to_thread(checker.request, 'check', {'id': request.id}, TIMEOUT)
    assert reply.payload == {'state': 'pending', 'seq': 0}
    checker.close()

    # the waiter's route survived the peek: the terminal still reaches it
    await harness.transport.send(
      harness.channel,
      Message(type='completed', payload={'result': 'mine'}, in_reply_to=request.id),
    )
    terminal = await asyncio.wait_for(waiter_task, TIMEOUT)
    assert terminal.payload == {'result': 'mine'}
    waiter.close()


@pytest.mark.asyncio
async def test_check_malformed_payload_replies_error():
  async with running_broxy() as harness:
    client = _local_client(harness)
    reply = await asyncio.to_thread(client.request, 'check', {'id': 7}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert 'string' in reply.payload['error']
    client.close()


@pytest.mark.asyncio
async def test_mailbox_bound_evicts_the_oldest_buffered_request():
  # the bound fits one filler frame (~410 bytes with envelope) but not two
  async with running_broxy(mailbox_bytes=500) as harness:
    first = await _detached_request(harness, 'summon')
    second = await _detached_request(harness, 'summon')

    filler = 'x' * 300
    await harness.transport.send(
      harness.channel, Message(type='completed', payload={'result': filler}, in_reply_to=first.id)
    )
    await _wait_until(
      lambda: harness.broxy._routes[first.id].message_bytes > 0,
      'the first terminal never reached the mailbox',
    )
    await harness.transport.send(
      harness.channel, Message(type='completed', payload={'result': filler}, in_reply_to=second.id)
    )
    await _wait_until(
      lambda: first.id not in harness.broxy._routes,
      'the over-bound mailbox never evicted the oldest request',
    )

    client = _local_client(harness)
    evicted = await asyncio.to_thread(client.request, 'claim', {'id': first.id}, TIMEOUT)
    assert evicted.type == Tag.REPLY
    assert first.id in evicted.payload['error']
    kept = await asyncio.to_thread(client.request, 'claim', {'id': second.id}, TIMEOUT)
    assert kept.type == 'completed'
    assert kept.payload == {'result': filler}
    client.close()


@pytest.mark.asyncio
async def test_live_delivered_terminal_stays_readable_through_a_cursor():
  # delivery no longer destroys the result: after a live request/reply exchange,
  # a plain check reports collected and a cursor read replays the conversation
  async with running_broxy() as harness:
    client = _local_client(harness)
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'summon', {}, TIMEOUT))
    _, request = await _next(harness.sink.messages)
    await harness.transport.send(
      harness.channel,
      Message(
        type='completed', payload={'result': 'ok', 'end_reason': 'ok'}, in_reply_to=request.id
      ),
    )
    await asyncio.wait_for(request_task, TIMEOUT)

    reply = await asyncio.to_thread(client.request, 'check', {'id': request.id}, TIMEOUT)
    assert reply.payload == {'state': 'collected', 'seq': 1}
    for _ in range(2):  # cursor reads are idempotent
      replayed = await asyncio.to_thread(
        client.call, 'check', {'id': request.id, 'last_seen': 0}, TIMEOUT
      )
      assert replayed.type == 'completed'
      assert replayed.payload == {'result': 'ok', 'end_reason': 'ok'}
    client.close()


@pytest.mark.asyncio
async def test_cursor_read_marks_read_and_spends_the_collect():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')
    await harness.transport.send(
      harness.channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await harness.transport.send(
      harness.channel,
      Message(
        type='completed', payload={'result': 'ok', 'end_reason': 'ok'}, in_reply_to=request.id
      ),
    )
    await _wait_until(
      lambda: harness.broxy._routes[request.id].terminal_seq is not None,
      'the terminal never reached the mailbox',
    )

    reader = _local_client(harness)
    interims: list[Message] = []
    terminal = await asyncio.to_thread(
      reader.call, 'check', {'id': request.id, 'last_seen': 0}, TIMEOUT, on_interim=interims.append
    )
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
    assert terminal.payload == {'result': 'ok', 'end_reason': 'ok'}

    # the cursor read acknowledged the window through its terminal: collect is spent
    reply = await asyncio.to_thread(reader.request, 'claim', {'id': request.id}, TIMEOUT)
    assert reply.type == Tag.REPLY
    assert 'already collected' in reply.payload['error']
    reader.close()


@pytest.mark.asyncio
async def test_cursor_read_pending_window_ends_with_a_state_reply():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')
    await harness.transport.send(
      harness.channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await _wait_until(
      lambda: len(harness.broxy._routes[request.id].messages) > 0,
      'the started interim never reached the mailbox',
    )

    reader = _local_client(harness)
    interims: list[Message] = []
    marker = await asyncio.to_thread(
      reader.call, 'check', {'id': request.id, 'last_seen': 0}, TIMEOUT, on_interim=interims.append
    )
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
    assert marker.type == Tag.REPLY
    assert marker.payload == {'state': 'pending', 'seq': 1, 'trail_id': 't1'}
    reader.close()


@pytest.mark.asyncio
async def test_cursor_read_from_the_future_errors():
  # reading from beyond read_up_to would acknowledge messages nobody has seen
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')
    await harness.transport.send(
      harness.channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await _wait_until(
      lambda: len(harness.broxy._routes[request.id].messages) > 0,
      'the started interim never reached the mailbox',
    )

    reader = _local_client(harness)
    reply = await asyncio.to_thread(
      reader.request, 'check', {'id': request.id, 'last_seen': 1}, TIMEOUT
    )
    assert reply.type == Tag.REPLY
    assert 'from the future' in reply.payload['error']
    reader.close()


@pytest.mark.asyncio
async def test_check_malformed_last_seen_replies_error():
  async with running_broxy() as harness:
    client = _local_client(harness)
    reply = await asyncio.to_thread(
      client.request, 'check', {'id': 'whatever', 'last_seen': -1}, TIMEOUT
    )
    assert reply.type == Tag.REPLY
    assert 'non-negative' in reply.payload['error']
    client.close()


@pytest.mark.asyncio
async def test_check_reports_collected_after_a_claim():
  async with running_broxy() as harness:
    request = await _detached_request(harness, 'summon')
    await harness.transport.send(
      harness.channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request.id)
    )
    await harness.transport.send(
      harness.channel,
      Message(
        type='completed', payload={'result': 'ok', 'end_reason': 'ok'}, in_reply_to=request.id
      ),
    )
    await _wait_until(
      lambda: harness.broxy._routes[request.id].terminal_seq is not None,
      'the terminal never reached the mailbox',
    )

    claimer = _local_client(harness)
    await asyncio.to_thread(claimer.call, 'claim', {'id': request.id}, TIMEOUT)
    reply = await asyncio.to_thread(claimer.request, 'check', {'id': request.id}, TIMEOUT)
    assert reply.payload == {'state': 'collected', 'seq': 2, 'trail_id': 't1'}
    # the conversation is still there for cursor re-reads
    terminal = await asyncio.to_thread(
      claimer.call, 'check', {'id': request.id, 'last_seen': 0}, TIMEOUT
    )
    assert terminal.payload == {'result': 'ok', 'end_reason': 'ok'}
    claimer.close()


@pytest.mark.asyncio
async def test_mailbox_bound_prefers_evicting_a_collected_conversation():
  # the bound fits one filler frame but not both conversations: the collected one
  # goes first even though the unread one is older
  async with running_broxy(mailbox_bytes=500) as harness:
    unread = await _detached_request(harness, 'summon')  # older, must survive
    collected = await _detached_request(harness, 'summon')
    await harness.transport.send(
      harness.channel,
      Message(type='completed', payload={'result': 'small'}, in_reply_to=collected.id),
    )
    collector = _local_client(harness)
    terminal = await asyncio.to_thread(collector.call, 'claim', {'id': collected.id}, TIMEOUT)
    assert terminal.payload == {'result': 'small'}
    collector.close()

    filler = 'x' * 300
    await harness.transport.send(
      harness.channel, Message(type='completed', payload={'result': filler}, in_reply_to=unread.id)
    )
    await _wait_until(
      lambda: collected.id not in harness.broxy._routes,
      'the over-bound mailbox never evicted the collected conversation',
    )
    assert unread.id in harness.broxy._routes

    client = _local_client(harness)
    kept = await asyncio.to_thread(client.request, 'claim', {'id': unread.id}, TIMEOUT)
    assert kept.payload == {'result': filler}
    client.close()


@pytest.mark.asyncio
async def test_mailbox_bound_never_evicts_a_live_wait():
  # an oversized interim on a live wait: no eviction candidate, so the bound is
  # exceeded rather than the wait broken
  async with running_broxy(mailbox_bytes=100) as harness:
    waiter = _local_client(harness)
    interims: list[Message] = []
    waiter_task = asyncio.create_task(
      asyncio.to_thread(waiter.call, 'summon', {}, TIMEOUT, on_interim=interims.append)
    )
    _, request = await _next(harness.sink.messages)
    await harness.transport.send(
      harness.channel,
      Message(type='started', payload={'trail_id': 'x' * 200}, in_reply_to=request.id),
    )
    await _wait_until(
      lambda: harness.broxy._retained_total > 100, 'the oversized interim never landed'
    )
    assert request.id in harness.broxy._routes

    await harness.transport.send(
      harness.channel, Message(type='completed', payload={'result': 'ok'}, in_reply_to=request.id)
    )
    terminal = await asyncio.wait_for(waiter_task, TIMEOUT)
    assert terminal.payload == {'result': 'ok'}
    assert len(interims) == 1
    waiter.close()


@pytest.mark.asyncio
async def test_malformed_local_frame_drops_only_that_connection():
  async with running_broxy() as harness:
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(str(harness.socket_path))
    raw.sendall(b'not json\n')
    raw.settimeout(TIMEOUT)
    assert await asyncio.to_thread(raw.recv, 1024) == b''  # the broxy closed it
    raw.close()

    client = _local_client(harness)
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    channel, message = await _next(harness.sink.messages)
    await harness.transport.send(channel, Message(type='reply', payload={}, in_reply_to=message.id))
    assert (await asyncio.wait_for(request_task, TIMEOUT)).type == 'reply'
    client.close()


@pytest.mark.asyncio
async def test_clean_stop_exits_zero_and_unlinks_the_socket():
  async with running_broxy() as harness:
    harness.broxy.stop()
    assert await asyncio.wait_for(harness.run_task, TIMEOUT) == 0
    assert not harness.socket_path.exists()


@pytest.mark.asyncio
async def test_upstream_eof_exits_nonzero_and_closes_local_connections():
  async with running_broxy() as harness:
    client = _local_client(harness)
    await harness.transport.close(harness.channel)
    assert await asyncio.wait_for(harness.run_task, TIMEOUT) == 1
    assert await asyncio.to_thread(client.receive, TIMEOUT) is None  # EOF
    client.close()


def test_launch_starts_serve_and_prints_address_and_pid(tmp_path, monkeypatch, capsys):
  process = MagicMock(pid=123)
  popen = MagicMock(return_value=process)
  monkeypatch.setattr(broker_broxy.spawn, 'popen', popen)
  monkeypatch.setattr(broker_broxy, '_await_ready', MagicMock(return_value=0))
  socket_path = tmp_path / 'broxy.sock'
  log_path = tmp_path / 'broxy.log'

  argv = [
    'broxy',
    'launch',
    str(socket_path),
    '--upstream',
    'unix:/upstream.sock',
    '--log-file',
    str(log_path),
  ]
  assert broker_broxy.main(argv) == 0
  assert capsys.readouterr().out == f'unix:{socket_path}\t123\n'
  call = popen.call_args
  assert call.args[0] == [
    'broxy',
    'serve',
    str(socket_path),
    '--upstream',
    'unix:/upstream.sock',
  ]
  assert call.kwargs['stderr'] == subprocess.STDOUT


def test_launch_stops_serve_when_readiness_fails(tmp_path, monkeypatch):
  process = MagicMock(pid=123)
  monkeypatch.setattr(broker_broxy.spawn, 'popen', MagicMock(return_value=process))
  monkeypatch.setattr(broker_broxy, '_await_ready', MagicMock(return_value=1))

  argv = [
    'broxy',
    'launch',
    str(tmp_path / 'broxy.sock'),
    '--upstream',
    'unix:/upstream.sock',
    '--log-file',
    str(tmp_path / 'broxy.log'),
  ]
  assert broker_broxy.main(argv) == 1
  process.terminate.assert_called_once_with()
  process.wait.assert_called_once_with(timeout=10)


def test_serve_requires_an_upstream(tmp_path, monkeypatch):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert broker_broxy.main(['broxy', 'serve', str(tmp_path / 'broxy.sock')]) == 1


def test_serve_rejects_a_non_unix_upstream(tmp_path):
  argv = ['broxy', 'serve', str(tmp_path / 'broxy.sock'), '--upstream', 'ws:x']
  assert broker_broxy.main(argv) == 1


def test_await_succeeds_on_a_listening_socket(socket_dir):
  path = socket_dir / 'ready.sock'
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  listener.bind(str(path))
  listener.listen(1)
  try:
    assert broker_broxy.main(['broxy', 'await', str(path)]) == 0
  finally:
    listener.close()


def test_await_times_out_on_a_missing_socket(socket_dir):
  argv = ['broxy', 'await', str(socket_dir / 'missing.sock'), '--timeout', '0.3']
  assert broker_broxy.main(argv) == 1
