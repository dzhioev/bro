import asyncio
import contextlib
import socket
import subprocess
from dataclasses import dataclass
from typing import cast
from unittest.mock import MagicMock

import pytest

import bro.broker.broxy as broker_broxy
from bro.broker import brotocol
from bro.broker.brotocol import PROTOCOL_REVISION, Message, Tag
from bro.broker.broxy import Broxy
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.dispatcher import QUERY, Dispatcher, query_handler
from bro.broker.runtime import Runtime
from bro.broker.spawn import Spawner
from bro.broker.transport import ChannelID, Sink, connect
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
async def test_marks_and_messages_keep_the_sticky_route_until_the_result():
  async with running_broxy() as harness:
    client = await _local_client(harness)
    interims: list[Message] = []
    call_task = asyncio.create_task(
      asyncio.to_thread(client.call, 'summon', {}, TIMEOUT, on_interim=interims.append)
    )

    channel, request = await _next(harness.sink.messages)
    await harness.transport.send(channel, brotocol.mark(request.id, 'accepted'))
    await harness.transport.send(channel, brotocol.message(request.id, {'step': 1}))
    await _wait_until(lambda: len(interims) == 2, 'the interim messages never reached the client')
    assert harness.broxy._routes[request.id].writer.is_closing() is False

    await harness.transport.send(channel, brotocol.result(request.id, 'ok', value='done'))
    result = await asyncio.wait_for(call_task, TIMEOUT)
    assert result.payload == {'outcome': 'ok', 'value': 'done'}
    assert [(message.type, message.payload) for message in interims] == [
      (Tag.MARK, {'transition': 'accepted'}),
      (Tag.MESSAGE, {'step': 1}),
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
    assert await asyncio.to_thread(raw.recv, 1024) == f'ok {PROTOCOL_REVISION}\n'.encode()
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


@pytest.mark.asyncio
async def test_reply_routes_to_the_connection_that_sent_the_question():
  async with running_broxy() as harness:
    asker = await _local_client(harness)
    question = await asyncio.to_thread(asker.message, 'quest', {'text': 'approve?'}, question=True)
    await _next(harness.sink.messages)

    reply = brotocol.message('quest', {'text': 'yes'}, reply_to=question.id)
    await harness.transport.send(harness.channel, reply)

    assert await asyncio.to_thread(asker.receive, TIMEOUT) == reply
    assert question.id in harness.broxy._routes
    asker.close()
    await _wait_until(
      lambda: question.id not in harness.broxy._routes,
      'the question route survived its connection',
    )


@pytest.mark.asyncio
async def test_unsolicited_messages_fan_out_to_every_live_listener():
  async with running_broxy() as harness:
    first = await _local_client(harness)
    second = await _local_client(harness)
    await asyncio.to_thread(first.listen, 'quest')
    await asyncio.to_thread(second.listen, 'quest')
    await _next(harness.sink.messages)
    await _next(harness.sink.messages)

    initial = brotocol.message('quest', {'index': 1})
    await harness.transport.send(harness.channel, initial)
    assert await asyncio.to_thread(first.receive, TIMEOUT) == initial
    assert await asyncio.to_thread(second.receive, TIMEOUT) == initial

    first.close()
    await _wait_until(
      lambda: len(harness.broxy._listeners.get('quest', ())) == 1,
      'the listener survived its connection',
    )
    later = brotocol.message('quest', {'index': 2}, reply_to='gone')
    await harness.transport.send(harness.channel, later)
    assert await asyncio.to_thread(second.receive, TIMEOUT) == later
    second.close()


@pytest.mark.asyncio
async def test_reply_lookup_prefers_correlation_then_quest_before_listeners():
  async with running_broxy() as harness:
    requester = await _local_client(harness)
    questioner = await _local_client(harness)
    listener = await _local_client(harness)
    sent = await asyncio.to_thread(requester.send, 'work', {})
    await _next(harness.sink.messages)
    question = await asyncio.to_thread(
      questioner.message,
      sent.quest_id,
      {},
      question=True,
    )
    await _next(harness.sink.messages)
    await asyncio.to_thread(listener.listen, sent.quest_id)
    await _next(harness.sink.messages)

    exact = brotocol.message(sent.quest_id, {'text': 'answer'}, reply_to=question.id)
    await harness.transport.send(harness.channel, exact)
    assert await asyncio.to_thread(questioner.receive, TIMEOUT) == exact
    assert await asyncio.to_thread(requester.receive, 0.05) is None
    assert await asyncio.to_thread(listener.receive, 0.05) is None

    fallback = brotocol.message(sent.quest_id, {'text': 'working'}, reply_to='gone')
    await harness.transport.send(harness.channel, fallback)
    assert await asyncio.to_thread(requester.receive, TIMEOUT) == fallback
    assert await asyncio.to_thread(listener.receive, 0.05) is None
    requester.close()
    questioner.close()
    listener.close()


@pytest.mark.asyncio
async def test_missing_reply_route_warns_while_a_listenerless_message_is_info(caplog):
  async with running_broxy() as harness:
    caplog.clear()
    reply = brotocol.message('quest', {}, reply_to='gone')
    unsolicited = brotocol.message('other-quest', {})
    await harness.transport.send(harness.channel, reply)
    await harness.transport.send(harness.channel, unsolicited)
    await _wait_until(
      lambda: 'other-quest' in caplog.text,
      'the listenerless message was not logged',
    )

    levels = {record.getMessage(): record.levelname for record in caplog.records}
    assert any('gone' in message and level == 'WARNING' for message, level in levels.items())
    assert any('other-quest' in message and level == 'INFO' for message, level in levels.items())


@pytest.mark.asyncio
async def test_route_bound_covers_request_question_and_listener_entries():
  async with running_broxy(max_routes=2) as harness:
    requester = await _local_client(harness)
    listener = await _local_client(harness)
    request = await asyncio.to_thread(requester.send, 'work', {})
    await _next(harness.sink.messages)
    await asyncio.to_thread(listener.listen, 'quest')
    await _next(harness.sink.messages)
    question = await asyncio.to_thread(
      requester.message,
      'quest',
      {'text': 'approve?'},
      question=True,
    )
    await _next(harness.sink.messages)

    assert request.id not in harness.broxy._routes
    assert question.id in harness.broxy._routes
    assert len(harness.broxy._listeners['quest']) == 1
    assert len(harness.broxy._registrations) == 2
    requester.close()
    listener.close()


class DispatcherSink:
  def __init__(self, dispatcher: Dispatcher):
    self.dispatcher = dispatcher

  async def on_connect(self, channel: ChannelID) -> None:
    pass

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    self.dispatcher.on_message(channel, message)

  async def on_disconnect(self, channel: ChannelID) -> None:
    pass


@dataclass
class ChatHarness:
  dispatcher: Dispatcher
  quest: str
  requester_address: str
  worker_address: str
  requester_broxy: Broxy
  worker_broxy: Broxy


@contextlib.asynccontextmanager
async def running_chat(talk):
  transport = TcpServerTransport([LOCAL_HOST])
  requester = await transport.provision()
  worker = await transport.provision()
  dispatcher = Dispatcher()
  dispatcher.bind(Runtime(transport, cast(Spawner, object())))
  dispatcher.on(QUERY, query_handler)
  root = dispatcher.journal.open('root', 'root', None, None, {})
  dispatcher.journal.bind(root, requester.channel)
  dispatcher.workers[requester.channel] = root.quest_id
  quest = 'chat-quest'
  record = dispatcher.journal.open(
    quest,
    'work',
    root.quest_id,
    requester.channel,
    {},
    talk=frozenset(talk),
  )
  dispatcher.journal.bind(record, worker.channel)
  dispatcher.live[quest] = record
  dispatcher.workers[worker.channel] = quest
  serve_task = asyncio.create_task(transport.serve(cast(Sink, DispatcherSink(dispatcher))))
  await asyncio.sleep(0)
  requester_broxy = Broxy(requester.host_endpoint.address(LOCAL_HOST))
  worker_broxy = Broxy(worker.host_endpoint.address(LOCAL_HOST))
  requester_ready: asyncio.Future = asyncio.get_running_loop().create_future()
  worker_ready: asyncio.Future = asyncio.get_running_loop().create_future()
  requester_task = asyncio.create_task(requester_broxy.run(requester_ready.set_result))
  worker_task = asyncio.create_task(worker_broxy.run(worker_ready.set_result))
  requester_address, worker_address = await asyncio.gather(requester_ready, worker_ready)
  try:
    yield ChatHarness(
      dispatcher,
      quest,
      requester_address,
      worker_address,
      requester_broxy,
      worker_broxy,
    )
  finally:
    requester_broxy.stop()
    worker_broxy.stop()
    await asyncio.gather(requester_task, worker_task)
    await transport.shutdown()
    await serve_task


@pytest.mark.asyncio
async def test_live_chat_crosses_dispatcher_and_broxies_in_both_directions():
  async with running_chat({'summoner.question', 'summoned.say'}) as harness:
    requester = Client(await asyncio.to_thread(connect, harness.requester_address))
    worker = Client(await asyncio.to_thread(connect, harness.worker_address))
    observer = Client(await asyncio.to_thread(connect, harness.worker_address))
    await asyncio.to_thread(worker.listen, harness.quest)
    await asyncio.to_thread(observer.listen, harness.quest)
    await _wait_until(
      lambda: len(harness.worker_broxy._listeners.get(harness.quest, ())) == 2,
      'the worker listeners were not registered',
    )

    question = await asyncio.to_thread(
      requester.message,
      harness.quest,
      {'command': 'inspect'},
      question=True,
    )
    delivered = await asyncio.to_thread(worker.receive, TIMEOUT)
    observed = await asyncio.to_thread(observer.receive, TIMEOUT)
    assert delivered == question
    assert observed == question

    reply = await asyncio.to_thread(
      worker.message,
      harness.quest,
      {'result': 'ready'},
      reply_to=question.id,
    )
    assert await asyncio.to_thread(requester.await_reply_to, question, TIMEOUT) == reply
    requester.close()
    worker.close()
    observer.close()


def _await_reply_through_journal(
  client: Client,
  query_client: Client,
  question: Message,
  *,
  since: int,
) -> Message:
  try:
    return client.await_reply_to(question, 0.05)
  except TimeoutError:
    response = query_client.call(
      QUERY,
      {'id': question.quest_id, 'wait': TIMEOUT, 'since': since},
      TIMEOUT * 2,
    )
  messages = response.payload['value']['quest']['messages']
  refusal = next(
    (message for message in messages if message.get('id') == question.id),
    None,
  )
  if refusal is None or 'reason' not in refusal:
    raise TimeoutError(f'no reply to message {question.id}')
  raise PermissionError(refusal['reason'])


@pytest.mark.asyncio
async def test_refused_live_question_is_correlated_in_the_journal():
  async with running_chat({'summoner.question', 'summoned.say'}) as harness:
    worker = Client(await asyncio.to_thread(connect, harness.worker_address))
    query = Client(await asyncio.to_thread(connect, harness.worker_address))
    before = harness.dispatcher.journal.records[harness.quest].chat_seq
    question = await asyncio.to_thread(
      worker.message,
      harness.quest,
      {'text': 'may I?'},
      question=True,
    )
    reason = 'summoned lacks the talk right for this message'
    with pytest.raises(PermissionError, match=reason):
      await asyncio.to_thread(
        _await_reply_through_journal,
        worker,
        query,
        question,
        since=before,
      )

    response = await asyncio.to_thread(
      query.call,
      QUERY,
      {'id': harness.quest, 'wait': TIMEOUT, 'since': before},
      TIMEOUT * 2,
    )
    refusal = next(
      message
      for message in response.payload['value']['quest']['messages']
      if message.get('id') == question.id
    )
    assert refusal['reason'] == reason
    worker.close()
    query.close()
