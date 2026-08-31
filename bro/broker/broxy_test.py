import asyncio
import contextlib
import socket
import subprocess
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

import bro.broker.broxy as broker_broxy
from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.broxy import Broxy
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.transport import ChannelID, connect
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport, parse_address

TIMEOUT = 5.0
_UPSTREAM_ADDRESS = 'tcp://upstream-token@127.0.0.1:9'
_LOCAL_ADDRESS = 'tcp://local-token@127.0.0.1:8'


class StubSink:
  def __init__(self):
    self.connects: asyncio.Queue = asyncio.Queue()
    self.messages: asyncio.Queue = asyncio.Queue()
    self.disconnects: asyncio.Queue = asyncio.Queue()

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
  channel: ChannelID
  address: str
  broxy: Broxy
  run_task: asyncio.Task


@contextlib.asynccontextmanager
async def running_broxy(**broxy_kwargs):
  transport = TcpServerTransport([LOCAL_HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)
  provisioned = await transport.provision()
  broxy = Broxy(provisioned.host_endpoint.address(LOCAL_HOST), **broxy_kwargs)
  listening: asyncio.Future = asyncio.get_running_loop().create_future()
  run_task = asyncio.create_task(broxy.run(listening.set_result))
  address = await asyncio.wait_for(listening, TIMEOUT)
  assert await asyncio.to_thread(broker_broxy._await_ready, address, TIMEOUT) == 0
  try:
    yield Harness(
      transport=transport,
      sink=sink,
      channel=provisioned.channel,
      address=address,
      broxy=broxy,
      run_task=run_task,
    )
  finally:
    broxy.stop()
    await asyncio.wait_for(run_task, TIMEOUT)
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


async def _wait_until(condition, message: str):
  deadline = asyncio.get_running_loop().time() + TIMEOUT
  while not bool(condition()):
    if asyncio.get_running_loop().time() > deadline:
      raise AssertionError(message)
    await asyncio.sleep(0.01)


async def _local_client(harness: Harness) -> Client:
  return Client(await asyncio.to_thread(connect, harness.address))


@pytest.mark.asyncio
async def test_request_round_trips_through_the_broxy():
  async with running_broxy() as harness:
    client = await _local_client(harness)
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {'n': 1}, TIMEOUT))

    channel, message = await _next(harness.sink.messages)
    assert channel == harness.channel
    assert message.kind == 'ping'
    assert message.args == {'n': 1}
    await harness.transport.send(channel, brotocol.result(message.id, 'ok', value={'pong': 1}))

    reply = await asyncio.wait_for(request_task, TIMEOUT)
    assert reply.type == Tag.RESULT
    assert reply.quest == message.id
    assert reply.payload == {'outcome': 'ok', 'value': {'pong': 1}}
    assert message.id not in harness.broxy._routes
    client.close()


@pytest.mark.asyncio
async def test_from_env_client_works_through_the_broxy(monkeypatch):
  async with running_broxy() as harness:
    monkeypatch.setenv(CHANNEL_ENV, harness.address)
    client = await asyncio.to_thread(Client.from_env)
    assert client is not None
    await asyncio.to_thread(client.send, 'ping', {'n': 1})
    channel, message = await _next(harness.sink.messages)
    assert channel == harness.channel
    assert message.payload == {'kind': 'ping', 'args': {'n': 1}}
    client.close()


@pytest.mark.asyncio
async def test_marks_and_progress_keep_the_sticky_route_until_the_result():
  async with running_broxy() as harness:
    client = await _local_client(harness)
    interims: list[Message] = []
    call_task = asyncio.create_task(
      asyncio.to_thread(client.call, 'summon', {}, TIMEOUT, on_interim=interims.append)
    )

    channel, request = await _next(harness.sink.messages)
    await harness.transport.send(channel, brotocol.mark(request.id, 'accepted'))
    await harness.transport.send(channel, brotocol.progress(request.id, {'step': 1}))
    await _wait_until(lambda: len(interims) == 2, 'the interim messages never reached the client')
    assert harness.broxy._routes[request.id].writer.is_closing() is False

    await harness.transport.send(channel, brotocol.result(request.id, 'ok', value='done'))
    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.payload == {'outcome': 'ok', 'value': 'done'}
    assert [(message.type, message.payload) for message in interims] == [
      (Tag.MARK, {'transition': 'accepted'}),
      (Tag.PROGRESS, {'step': 1}),
    ]
    assert request.id not in harness.broxy._routes
    client.close()


@pytest.mark.asyncio
async def test_concurrent_local_clients_share_one_upstream_and_route_stickily():
  async with running_broxy() as harness:
    assert await _next(harness.sink.connects) == harness.channel
    client_a = await _local_client(harness)
    client_b = await _local_client(harness)
    task_a = asyncio.create_task(
      asyncio.to_thread(client_a.request, 'ping', {'from': 'a'}, TIMEOUT)
    )
    request_a = (await _next(harness.sink.messages))[1]
    task_b = asyncio.create_task(
      asyncio.to_thread(client_b.request, 'ping', {'from': 'b'}, TIMEOUT)
    )
    request_b = (await _next(harness.sink.messages))[1]
    assert harness.sink.connects.empty()

    await harness.transport.send(
      harness.channel, brotocol.result(request_b.id, 'ok', value={'to': 'b'})
    )
    await harness.transport.send(
      harness.channel, brotocol.result(request_a.id, 'ok', value={'to': 'a'})
    )
    assert (await asyncio.wait_for(task_b, TIMEOUT)).payload['value'] == {'to': 'b'}
    assert (await asyncio.wait_for(task_a, TIMEOUT)).payload['value'] == {'to': 'a'}
    client_a.close()
    client_b.close()


@pytest.mark.parametrize('request_kind', ['claim', 'check'])
@pytest.mark.asyncio
async def test_claim_and_check_requests_are_forwarded_upstream(request_kind):
  async with running_broxy() as harness:
    client = await _local_client(harness)
    request_task = asyncio.create_task(
      asyncio.to_thread(client.request, request_kind, {'id': 'quest'}, TIMEOUT)
    )
    channel, request = await _next(harness.sink.messages)
    assert request.kind == request_kind
    await harness.transport.send(
      channel, brotocol.result(request.id, 'denied', error=f'unknown kind {request_kind}')
    )
    reply = await asyncio.wait_for(request_task, TIMEOUT)
    assert reply.payload == {'outcome': 'denied', 'error': f'unknown kind {request_kind}'}
    client.close()


@pytest.mark.asyncio
async def test_local_half_close_forwards_every_request_and_removes_its_routes():
  async with running_broxy() as harness:
    local_transport = await asyncio.to_thread(connect, harness.address)
    requests = [brotocol.request('ping', {'index': index}) for index in range(2)]
    for request in requests:
      await asyncio.to_thread(local_transport.send, request)
    await asyncio.to_thread(local_transport.close, True)

    seen = [(await _next(harness.sink.messages))[1] for _ in requests]
    assert [message.id for message in seen] == [request.id for request in requests]
    assert all(request.id not in harness.broxy._routes for request in requests)


@pytest.mark.asyncio
async def test_message_for_a_disconnected_waiter_is_dropped(caplog):
  async with running_broxy() as harness:
    local_transport = await asyncio.to_thread(connect, harness.address)
    request = brotocol.request('ping', {})
    await asyncio.to_thread(local_transport.send, request)
    await asyncio.to_thread(local_transport.close, True)
    await _next(harness.sink.messages)

    await harness.transport.send(harness.channel, brotocol.result(request.quest_id, 'ok'))
    await _wait_until(lambda: request.quest_id in caplog.text, 'the dropped frame was not reported')
    assert request.quest_id not in harness.broxy._routes


@pytest.mark.asyncio
async def test_route_bound_drops_the_oldest_route():
  async with running_broxy(max_routes=1) as harness:
    first = await asyncio.to_thread(connect, harness.address)
    first_request = brotocol.request('ping', {'index': 1})
    await asyncio.to_thread(first.send, first_request)
    await _next(harness.sink.messages)

    second = await asyncio.to_thread(connect, harness.address)
    second_request = brotocol.request('ping', {'index': 2})
    await asyncio.to_thread(second.send, second_request)
    await _next(harness.sink.messages)
    assert list(harness.broxy._routes) == [second_request.quest_id]

    await harness.transport.send(harness.channel, brotocol.result(first_request.quest_id, 'ok'))
    assert await asyncio.to_thread(first.receive, 0.05) is None
    await harness.transport.send(
      harness.channel, brotocol.result(second_request.quest_id, 'ok', value='second')
    )
    result = await asyncio.to_thread(second.receive, TIMEOUT)
    assert result is not None
    assert result.payload == {'outcome': 'ok', 'value': 'second'}
    first.close()
    second.close()


def test_route_bound_must_be_positive():
  with pytest.raises(ValueError, match='positive'):
    Broxy(_UPSTREAM_ADDRESS, max_routes=0)


@pytest.mark.asyncio
async def test_local_token_authenticates_every_connection():
  async with running_broxy() as harness:
    host, port, _ = parse_address(harness.address)
    raw = socket.create_connection((host, port), timeout=TIMEOUT)
    raw.sendall(b'wrong-token\n')
    assert await asyncio.to_thread(raw.recv, 1024) == b''
    raw.close()

    client = await _local_client(harness)
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    channel, request = await _next(harness.sink.messages)
    await harness.transport.send(channel, brotocol.result(request.id, 'ok'))
    assert (await asyncio.wait_for(request_task, TIMEOUT)).type == Tag.RESULT
    client.close()


@pytest.mark.asyncio
async def test_malformed_local_frame_drops_only_that_connection():
  async with running_broxy() as harness:
    host, port, token = parse_address(harness.address)
    raw = socket.create_connection((host, port), timeout=TIMEOUT)
    raw.sendall(token.encode() + b'\n')
    assert await asyncio.to_thread(raw.recv, 1024) == b'ok\n'
    raw.sendall(b'not json\n')
    assert await asyncio.to_thread(raw.recv, 1024) == b''
    raw.close()

    client = await _local_client(harness)
    request_task = asyncio.create_task(asyncio.to_thread(client.request, 'ping', {}, TIMEOUT))
    channel, message = await _next(harness.sink.messages)
    await harness.transport.send(channel, brotocol.result(message.id, 'ok'))
    assert (await asyncio.wait_for(request_task, TIMEOUT)).type == Tag.RESULT
    client.close()


@pytest.mark.asyncio
async def test_clean_stop_exits_zero_and_stops_serving():
  async with running_broxy() as harness:
    harness.broxy.stop()
    assert await asyncio.wait_for(harness.run_task, TIMEOUT) == 0
    with pytest.raises(OSError):
      await asyncio.to_thread(connect, harness.address)


@pytest.mark.asyncio
async def test_upstream_eof_exits_nonzero_and_closes_local_connections():
  async with running_broxy() as harness:
    client = await _local_client(harness)
    await harness.transport.close(harness.channel)
    assert await asyncio.wait_for(harness.run_task, TIMEOUT) == 1
    assert await asyncio.to_thread(client.receive, TIMEOUT) is None
    client.close()


def test_launch_starts_serve_and_prints_address_and_pid(tmp_path, monkeypatch, capsys):
  process = MagicMock(pid=123)
  popen = MagicMock(return_value=process)
  monkeypatch.setattr(broker_broxy.spawn, 'popen', popen)
  monkeypatch.setattr(broker_broxy, '_await_address', MagicMock(return_value=_LOCAL_ADDRESS))
  monkeypatch.setattr(broker_broxy, '_await_ready', MagicMock(return_value=0))
  log_path = tmp_path / 'broxy.log'

  argv = ['broxy', 'launch', '--upstream', _UPSTREAM_ADDRESS, '--log-file', str(log_path)]
  assert broker_broxy.main(argv) == 0
  assert capsys.readouterr().out == f'{_LOCAL_ADDRESS}\t123\n'
  command = popen.call_args.args[0]
  assert command[:4] == ['broxy', 'serve', '--upstream', _UPSTREAM_ADDRESS]
  assert command[4] == '--address-file'
  assert popen.call_args.kwargs['stderr'] == subprocess.STDOUT


def test_launch_stops_serve_when_readiness_fails(tmp_path, monkeypatch):
  process = MagicMock(pid=123)
  monkeypatch.setattr(broker_broxy.spawn, 'popen', MagicMock(return_value=process))
  monkeypatch.setattr(broker_broxy, '_await_address', MagicMock(return_value=_LOCAL_ADDRESS))
  monkeypatch.setattr(broker_broxy, '_await_ready', MagicMock(return_value=1))

  argv = ['broxy', 'launch', '--upstream', _UPSTREAM_ADDRESS, '--log-file', str(tmp_path / 'l')]
  assert broker_broxy.main(argv) == 1
  process.terminate.assert_called_once_with()
  process.wait.assert_called_once_with(timeout=10)


def test_launch_fails_when_serve_dies_before_reporting_an_address(tmp_path, monkeypatch):
  process = MagicMock(pid=123, returncode=1)
  process.poll.return_value = 1
  monkeypatch.setattr(broker_broxy.spawn, 'popen', MagicMock(return_value=process))

  argv = ['broxy', 'launch', '--upstream', _UPSTREAM_ADDRESS, '--log-file', str(tmp_path / 'l')]
  assert broker_broxy.main(argv) == 1


def test_serve_requires_an_upstream(monkeypatch):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert broker_broxy.main(['broxy', 'serve']) == 1


def test_serve_rejects_an_upstream_that_is_no_channel_address():
  assert broker_broxy.main(['broxy', 'serve', '--upstream', 'ws:x']) == 1


@pytest.mark.asyncio
async def test_await_succeeds_on_a_listening_broxy():
  async with running_broxy() as harness:
    assert await asyncio.to_thread(broker_broxy.main, ['broxy', 'await', harness.address]) == 0


def test_await_times_out_on_a_dead_address():
  argv = ['broxy', 'await', _LOCAL_ADDRESS, '--timeout', '0.3']
  assert broker_broxy.main(argv) == 1
