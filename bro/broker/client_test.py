import asyncio
import contextlib
from dataclasses import dataclass

import pytest

from bro.broker import brotocol
from bro.broker.brotocol import Message
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.transport import ChannelID
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


def test_from_env_returns_none_when_unset(monkeypatch):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert Client.from_env() is None


@pytest.mark.asyncio
async def test_from_env_connects_and_sends(monkeypatch):
  async with running_server() as server:
    provisioned = await server.transport.provision()
    monkeypatch.setenv(CHANNEL_ENV, provisioned.host_endpoint.address(LOCAL_HOST))
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
async def test_progress_and_result_emit_against_an_exchange():
  # the answering side: a worker emits progress and the closing result against
  # the exchange id its launch carried
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    await asyncio.to_thread(client.progress, 'X', {'trail_id': 't1'})
    await asyncio.to_thread(client.result, 'X', {'outcome': 'ok', 'value': 'answer'})

    _, started = await _next(server.sink.messages)
    _, done = await _next(server.sink.messages)
    assert (started.type, started.request, started.payload) == ('progress', 'X', {'trail_id': 't1'})
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
    unrelated = brotocol.progress('some-other-exchange', {'note': 'unrelated'})
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
async def test_call_surfaces_progress_and_returns_the_result():
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
    await server.transport.send(channel, brotocol.progress(request_message.id, {'trail_id': 't1'}))
    unrelated = brotocol.progress('some-other-exchange', {'note': 'unrelated'})
    await server.transport.send(channel, unrelated)
    await server.transport.send(channel, brotocol.result(request_message.id, 'ok', value='r'))

    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.type == 'result'
    assert result.payload == {'outcome': 'ok', 'value': 'r'}
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]

    # the uncorrelated message call() read past was set aside, not dropped
    set_aside = await asyncio.to_thread(client.receive, 0.2)
    assert set_aside == unrelated
    client.close()


@pytest.mark.asyncio
async def test_call_rides_every_progress_while_await_any_returns_the_first():
  # every correlated progress rides through a call's wait; await_any is the one
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
    await server.transport.send(channel, brotocol.progress(request_message.id, {}))
    await server.transport.send(channel, brotocol.progress(request_message.id, {'trail_id': 't1'}))
    await server.transport.send(channel, brotocol.result(request_message.id, 'ok', value='r'))
    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.type == 'result'
    assert [interim.payload for interim in interims] == [{}, {'trail_id': 't1'}]

    sent = client.send('summon', {})
    any_task = asyncio.create_task(asyncio.to_thread(client.await_any, sent, TIMEOUT))
    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.progress(request_message.id, {}))
    first = await asyncio.wait_for(any_task, TIMEOUT)
    assert (first.type, first.request) == ('progress', sent.id)
    client.close()


@pytest.mark.asyncio
async def test_call_without_callback_skips_progress_and_returns_failed():
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    call_task = asyncio.create_task(asyncio.to_thread(client.call, 'summon', {}, TIMEOUT))

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.progress(request_message.id, {'trail_id': 't'}))
    await server.transport.send(
      channel, brotocol.result(request_message.id, 'failed', detail={'reason': 'exit'})
    )

    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.type == 'result'
    assert result.payload == {'outcome': 'failed', 'detail': {'reason': 'exit'}}
    client.close()


@pytest.mark.asyncio
async def test_call_deadline_spans_interim_progress():
  # `timeout` bounds the whole call: an interim progress does not extend the result wait.
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    call_task = asyncio.create_task(asyncio.to_thread(client.call, 'summon', {}, 0.3))

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.progress(request_message.id, {}))
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
    await server.transport.send(channel, brotocol.progress(request_message.id, {'trail_id': 't1'}))
    await server.transport.send(channel, brotocol.result(request_message.id, 'ok', value='r'))

    result = await asyncio.wait_for(await_task, TIMEOUT)
    assert result.type == 'result'
    assert [interim.payload for interim in interims] == [{'trail_id': 't1'}]
    client.close()


@pytest.mark.asyncio
async def test_await_reply_progress_rearms_the_deadline():
  # timeout_after_interim opts out of the whole-wait bound: a correlated progress
  # re-arms the deadline, so a result past the initial bound still lands
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    sent = await asyncio.to_thread(client.send, 'summon', {})
    await_task = asyncio.create_task(
      asyncio.to_thread(client.await_reply, sent, 0.3, timeout_after_interim=TIMEOUT)
    )

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.progress(request_message.id, {}))
    await asyncio.sleep(0.5)  # outlive the initial 0.3s bound; the re-armed deadline holds
    await server.transport.send(channel, brotocol.result(request_message.id, 'ok', value='r'))

    result = await asyncio.wait_for(await_task, TIMEOUT)
    assert result.type == 'result'
    client.close()


@pytest.mark.asyncio
async def test_await_reply_progress_rearm_shortens_a_longer_bound():
  # the re-arm is to exactly now + timeout_after_interim, shortening a still-long
  # initial bound too, so post-progress silence is caught at the tighter bound
  async with running_server() as server:
    provisioned = await server.transport.provision()
    client = Client(await _transport(provisioned))
    sent = await asyncio.to_thread(client.send, 'summon', {})
    await_task = asyncio.create_task(
      asyncio.to_thread(client.await_reply, sent, TIMEOUT * 4, timeout_after_interim=0.2)
    )

    channel, request_message = await _next(server.sink.messages)
    await server.transport.send(channel, brotocol.progress(request_message.id, {}))
    with pytest.raises(TimeoutError, match='within 0.2s'):
      await asyncio.wait_for(await_task, TIMEOUT)
    client.close()


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
