import asyncio
import contextlib
from dataclasses import dataclass

import pytest

from broker.brotocol import Message
from broker.client import CHANNEL_ENV, Client
from broker.transport import ChannelID
from broker.transports.unix import UnixClientTransport, UnixServerTransport

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


@contextlib.asynccontextmanager
async def running_server(socket_dir):
  transport = UnixServerTransport(str(socket_dir))
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


def test_from_env_returns_none_when_unset(monkeypatch):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert Client.from_env() is None


@pytest.mark.asyncio
async def test_from_env_connects_and_sends(socket_dir, monkeypatch):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
    client = Client.from_env()
    assert client is not None

    await asyncio.to_thread(client.send, 'started', {'trail_id': 't1'})
    channel, message = await _next(server.sink.messages)
    assert channel == provisioned.channel
    assert message.type == 'started'
    assert message.payload == {'trail_id': 't1'}
    assert message.in_reply_to is None
    client.close()


@pytest.mark.asyncio
async def test_request_correlates_and_sets_unrelated_aside(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {'n': 1}, TIMEOUT))

    channel, request_message = await _next(server.sink.messages)
    assert request_message.type == 'ping'
    unrelated = Message(type='status', payload={'note': 'unrelated'})
    await server.transport.send(channel, unrelated)
    await server.transport.send(
      channel, Message(type='reply', payload={'pong': 1}, in_reply_to=request_message.id)
    )

    reply = await asyncio.wait_for(request_task, TIMEOUT)
    assert reply.type == 'reply'
    assert reply.in_reply_to == request_message.id
    assert reply.payload == {'pong': 1}

    # the unrelated message request() read past was set aside, not dropped
    set_aside = await asyncio.to_thread(client.receive, 0.2)
    assert set_aside is not None
    assert set_aside.id == unrelated.id
    client.close()


@pytest.mark.asyncio
async def test_request_times_out_without_reply(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    with pytest.raises(TimeoutError):
      await asyncio.to_thread(client.request, 'ping', {}, 0.2)
    client.close()


@pytest.mark.asyncio
async def test_request_raises_on_channel_close(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    await _next(server.sink.messages)  # the request reached the host

    await server.transport.close(provisioned.channel)
    with pytest.raises(ConnectionError):
      await asyncio.wait_for(request_task, TIMEOUT)
    client.close()


@pytest.mark.asyncio
async def test_call_surfaces_started_and_returns_terminal(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    interims: list[Message] = []
    call_task = asyncio.create_task(
      asyncio.to_thread(
        client.call, 'summon', {'target': 'devoops'}, TIMEOUT, on_started=interims.append
      )
    )

    channel, request_message = await _next(server.sink.messages)
    assert request_message.type == 'summon'
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request_message.id)
    )
    unrelated = Message(type='status', payload={'note': 'unrelated'})
    await server.transport.send(channel, unrelated)
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'ok', 'end_reason': 'ok'},
        in_reply_to=request_message.id,
      ),
    )

    terminal = await asyncio.wait_for(call_task, TIMEOUT)
    assert terminal.type == 'completed'
    assert terminal.payload == {'result': 'ok', 'end_reason': 'ok'}
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]

    # the uncorrelated message call() read past was set aside, not dropped
    set_aside = await asyncio.to_thread(client.receive, 0.2)
    assert set_aside is not None
    assert set_aside.id == unrelated.id
    client.close()


@pytest.mark.asyncio
async def test_call_without_callback_skips_started_and_returns_failed(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    call_task = asyncio.create_task(asyncio.to_thread(client.call, 'summon', {}, TIMEOUT))

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 't'}, in_reply_to=request_message.id)
    )
    await server.transport.send(
      channel, Message(type='failed', payload={'reason': 'exit'}, in_reply_to=request_message.id)
    )

    terminal = await asyncio.wait_for(call_task, TIMEOUT)
    assert terminal.type == 'failed'
    assert terminal.payload == {'reason': 'exit'}
    client.close()


@pytest.mark.asyncio
async def test_call_deadline_spans_interim_started(socket_dir):
  # `timeout` bounds the whole call: an interim started does not extend the terminal wait.
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    call_task = asyncio.create_task(asyncio.to_thread(client.call, 'summon', {}, 0.3))

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='started', payload={}, in_reply_to=request_message.id)
    )
    with pytest.raises(TimeoutError):
      await asyncio.wait_for(call_task, TIMEOUT)
    client.close()


@pytest.mark.asyncio
async def test_send_returns_the_sent_message(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    sent = await asyncio.to_thread(client.send, 'summon', {'target': 'devoops'})

    _, received = await _next(server.sink.messages)
    # the id is minted client-side, so the caller holds it before any reply exists
    assert received.id == sent.id
    assert received.type == 'summon'
    client.close()


@pytest.mark.asyncio
async def test_await_reply_reattaches_to_a_sent_request(socket_dir):
  # send + await_reply is call() split in two: the id is exposed between them
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    sent = await asyncio.to_thread(client.send, 'summon', {'target': 'devoops'})
    interims: list[Message] = []
    await_task = asyncio.create_task(
      asyncio.to_thread(client.await_reply, sent, TIMEOUT, on_started=interims.append)
    )

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 't1'}, in_reply_to=request_message.id)
    )
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'ok', 'end_reason': 'ok'},
        in_reply_to=request_message.id,
      ),
    )

    terminal = await asyncio.wait_for(await_task, TIMEOUT)
    assert terminal.type == 'completed'
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
    client.close()


@pytest.mark.asyncio
async def test_await_reply_started_rearms_the_deadline(socket_dir):
  # timeout_after_started opts out of the whole-wait bound: the interim started
  # re-arms the deadline, so a terminal past the initial bound still lands
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    sent = await asyncio.to_thread(client.send, 'summon', {})
    await_task = asyncio.create_task(
      asyncio.to_thread(client.await_reply, sent, 0.3, timeout_after_started=TIMEOUT)
    )

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='started', payload={}, in_reply_to=request_message.id)
    )
    await asyncio.sleep(0.5)  # outlive the initial 0.3s bound; the re-armed deadline holds
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'ok', 'end_reason': 'ok'},
        in_reply_to=request_message.id,
      ),
    )

    terminal = await asyncio.wait_for(await_task, TIMEOUT)
    assert terminal.type == 'completed'
    client.close()


@pytest.mark.asyncio
async def test_await_reply_started_rearm_shortens_a_longer_bound(socket_dir):
  # the re-arm is to exactly now + timeout_after_started, shortening a still-long
  # initial bound too, so post-started silence is caught at the tighter bound
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    sent = await asyncio.to_thread(client.send, 'summon', {})
    await_task = asyncio.create_task(
      asyncio.to_thread(client.await_reply, sent, TIMEOUT * 4, timeout_after_started=0.2)
    )

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='started', payload={}, in_reply_to=request_message.id)
    )
    with pytest.raises(TimeoutError, match='within 0.2s'):
      await asyncio.wait_for(await_task, TIMEOUT)
    client.close()


@pytest.mark.asyncio
async def test_close_confirm_returns_after_the_host_consumed_everything(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    await asyncio.to_thread(client.send, 'completed', {'result': 'ok'})
    await asyncio.wait_for(asyncio.to_thread(client.close, True), TIMEOUT)
    # the host closes back only after its read loop consumed the frame, so the
    # message must already be here — no await
    assert server.sink.messages.qsize() == 1


@pytest.mark.asyncio
async def test_close_aborts_a_blocked_wait_from_another_thread(socket_dir):
  # the cross-thread abort guarantee (ClientTransport.close): a controller that
  # abandoned an off-thread wait closes the client, and the blocked receive
  # returns as channel EOF instead of hanging until traffic arrives
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    await _next(server.sink.messages)  # the request reached the host; the wait is blocked

    await asyncio.to_thread(client.close)
    with pytest.raises(ConnectionError):
      await asyncio.wait_for(request_task, TIMEOUT)


@pytest.mark.asyncio
async def test_receive_returns_none_on_timeout(socket_dir):
  async with running_server(socket_dir) as server:
    provisioned = await server.transport.provision()
    client = Client(UnixClientTransport(provisioned.host_endpoint))
    assert await asyncio.to_thread(client.receive, 0.2) is None
    client.close()
