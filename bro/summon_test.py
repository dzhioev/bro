import asyncio
import contextlib
import json
import os
from dataclasses import dataclass

import pytest

from bro import summon
from bro.broker import brotocol
from bro.broker.brotocol import Message
from bro.broker.client import CHANNEL_ENV
from bro.broker.transport import ChannelID
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport

TIMEOUT = 5.0


class StubSink:
  def __init__(self):
    self.messages: asyncio.Queue = asyncio.Queue()

  async def on_connect(self, channel: ChannelID) -> None:
    pass

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    self.messages.put_nowait((channel, message))

  async def on_disconnect(self, channel: ChannelID) -> None:
    pass


@dataclass
class Harness:
  transport: TcpServerTransport
  sink: StubSink


@contextlib.asynccontextmanager
async def running_server(monkeypatch):
  transport = TcpServerTransport([LOCAL_HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)
  provisioned = await transport.provision()
  monkeypatch.setenv(CHANNEL_ENV, provisioned.host_endpoint.address(LOCAL_HOST))
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _next(server: Harness) -> tuple[ChannelID, Message]:
  return await asyncio.wait_for(server.sink.messages.get(), TIMEOUT)


def _id(message: Message) -> str:
  assert message.id is not None
  return message.id


async def _reply(server: Harness, channel: ChannelID, request: Message, **payload) -> None:
  await server.transport.send(channel, brotocol.result(_id(request), **payload))


def _quest(
  request_id: str,
  state: str,
  *,
  result: dict | None = None,
  kind: str = 'summon',
  trail_id: str | None = None,
) -> dict:
  quest = {
    'id': request_id,
    'kind': kind,
    'parent': 'ROOT',
    'args': {'target': 'dev', 'prompt': 'work'},
    'state': state,
  }
  if result is not None:
    quest['result'] = result
  if trail_id is not None:
    quest['trail_id'] = trail_id
  return quest


def test_help_names_check_list_and_no_cursor_option(capsys):
  with pytest.raises(SystemExit):
    summon.main(['summon', 'check', '--help'])
  output = capsys.readouterr().out
  assert '--wait' in output
  assert '--last-seen' not in output

  with pytest.raises(SystemExit):
    summon.main(['summon', '--help'])
  output = capsys.readouterr().out
  assert 'summon check' in output
  assert 'summon list' in output


def test_bare_summon_forwards_with_its_own_shell_command(monkeypatch):
  calls: list[tuple] = []
  monkeypatch.delenv('BRO_SHELL_COMMAND', raising=False)
  monkeypatch.setattr(
    summon,
    'relay_summon',
    lambda target, prompt, *, timeout, into, hold, grant, revoke, share, llm, harness, manual: (
      calls.append((target, prompt, timeout, into)) or 0
    ),
  )

  assert summon.main(['summon', '--timeout', '60', 'dev', 'deploy']) == 0
  assert calls == [('dev', 'deploy', 60.0, None)]
  assert os.environ['BRO_SHELL_COMMAND'] == 'summon --timeout 60.0 dev deploy'


def test_manual_summon_refuses_launch_owned_flags(monkeypatch, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'tcp://token@127.0.0.1:1')
  for flags in (
    ['--timeout', '60'],
    ['--hold', 'attended'],
    ['--harness', 'claude'],
    ['--llm', ':fable5'],
  ):
    assert summon.main(['summon', '--manual', *flags, 'dev', 'work']) == 1
  assert sum('launch owns' in record.getMessage() for record in caplog.records) == 4


@pytest.mark.asyncio
async def test_detached_summon_waits_for_acceptance(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--detach', '--timeout', '42', 'dev', 'work'])
    )
    channel, request = await _next(server)
    assert request.kind == 'summon'
    assert request.args == {'target': 'dev', 'prompt': 'work', 'timeout': 42.0}
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))

    assert await task == 0
    assert capsys.readouterr().out == f'{request.id}\n'


@pytest.mark.asyncio
async def test_denied_detached_summon_fails_without_printing_an_id(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--detach', 'dev', 'work'])
    )
    channel, request = await _next(server)
    await _reply(
      server,
      channel,
      request,
      outcome='denied',
      error="summon denied: 'dev' is not in the list",
    )

    assert await task == 1
    assert capsys.readouterr().out == ''
    assert 'summon denied' in caplog.text


@pytest.mark.asyncio
async def test_manual_detached_summon_returns_launch_token_after_acceptance(
  monkeypatch, capsys, caplog
):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--manual', '--detach', 'dev', 'work'])
    )
    channel, request = await _next(server)
    assert request.args == {'target': 'dev', 'prompt': 'work', 'manual': True}
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))

    assert await task == 0
    assert capsys.readouterr().out == f'{request.id}\n'
    assert summon.manual_launch_command(_id(request), 'dev') in caplog.text


@pytest.mark.asyncio
async def test_blocking_summon_relays_the_answer(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'dev', 'work']))
    channel, request = await _next(server)
    await server.transport.send(channel, brotocol.mark(_id(request), 'accepted'))
    await server.transport.send(channel, brotocol.mark(_id(request), 'trail', trail_id='T1'))
    await _reply(server, channel, request, outcome='ok', value='answer')

    assert await task == 0
    assert capsys.readouterr().out == 'answer\n'
    assert request.id in caplog.text
    assert 'T1' in caplog.text


@pytest.mark.asyncio
async def test_silent_blocking_wait_queries_live_state_then_resumes(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--timeout', '0.05', 'dev', 'work'])
    )
    channel, original = await _next(server)
    await server.transport.send(channel, brotocol.mark(_id(original), 'accepted'))

    query_channel, query = await _next(server)
    assert query.kind == 'query'
    assert query.args == {'id': original.id}
    await server.transport.send(query_channel, brotocol.result(_id(original), 'ok', value='answer'))
    await _reply(
      server,
      query_channel,
      query,
      outcome='ok',
      value={'quest': _quest(_id(original), 'started', trail_id='T2')},
    )

    assert await task == 0
    assert capsys.readouterr().out == 'answer\n'


@pytest.mark.asyncio
async def test_silent_blocking_wait_interprets_terminal_query(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', '--timeout', '0.05', 'dev', 'work'])
    )
    _, original = await _next(server)
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          _id(original),
          'ended',
          result={'outcome': 'ok', 'value': 'retained answer'},
        )
      },
    )

    assert await task == 0
    assert capsys.readouterr().out == 'retained answer\n'


@pytest.mark.asyncio
async def test_check_queries_pending_without_consuming(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))
    channel, query = await _next(server)
    assert query.kind == 'query'
    assert query.args == {'id': 'REQ-1'}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'quest': _quest('REQ-1', 'started', trail_id='T9')},
    )

    assert await task == summon.PENDING_EXIT_CODE
    assert 'still running' in caplog.text
    assert 'T9' in caplog.text


@pytest.mark.asyncio
async def test_check_returns_the_retained_answer_repeatably(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    for _ in range(2):
      task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))
      channel, query = await _next(server)
      await _reply(
        server,
        channel,
        query,
        outcome='ok',
        value={
          'quest': _quest(
            'REQ-1',
            'ended',
            result={'outcome': 'ok', 'value': 'retained answer'},
          )
        },
      )
      assert await task == 0
    assert capsys.readouterr().out == 'retained answer\nretained answer\n'


@pytest.mark.asyncio
async def test_check_wait_loops_query_until_terminal(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'check', '--wait', '--timeout', '0.05', 'REQ-1'])
    )
    channel, query = await _next(server)
    assert query.args == {'id': 'REQ-1', 'wait': 0.05}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'quest': _quest('REQ-1', 'started')},
    )
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quest': _quest(
          'REQ-1',
          'ended',
          result={'outcome': 'ok', 'value': 'done'},
        )
      },
    )

    assert await task == 0
    assert capsys.readouterr().out == 'done\n'


@pytest.mark.asyncio
async def test_unknown_and_evicted_checks_fail(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    unknown = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'UNKNOWN']))
    channel, query = await _next(server)
    await _reply(server, channel, query, outcome='denied', error="unknown quest id 'UNKNOWN'")
    assert await unknown == 1

    evicted = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'OLD']))
    channel, query = await _next(server)
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={'quest': _quest('OLD', 'evicted')},
    )
    assert await evicted == 1
  assert 'unknown quest id' in caplog.text
  assert 'no longer retained' in caplog.text


@pytest.mark.asyncio
async def test_list_reads_every_page_and_keeps_only_summons(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'list']))
    channel, query = await _next(server)
    assert query.args == {}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quests': [_quest('ROOT', 'started', kind='root'), _quest('S1', 'started')],
        'cursor': 'NEXT',
      },
    )
    channel, query = await _next(server)
    assert query.args == {'cursor': 'NEXT'}
    await _reply(
      server,
      channel,
      query,
      outcome='ok',
      value={
        'quests': [
          _quest('B1', 'ended', kind='benchmark'),
          _quest('S0', 'denied', result={'outcome': 'denied', 'error': 'no'}),
        ]
      },
    )

    assert await task == 0
    assert [quest['id'] for quest in json.loads(capsys.readouterr().out)['quests']] == ['S1', 'S0']


@pytest.mark.asyncio
async def test_watch_arms_at_head_and_prints_ordered_summon_transitions(monkeypatch):
  async with running_server(monkeypatch) as server:
    watch = summon.watch_summons(wait_seconds=0.05)
    first_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, arm = await _next(server)
    assert arm.kind == 'events'
    assert arm.args == {}
    await _reply(server, channel, arm, outcome='ok', value={'head': 10, 'events': []})
    channel, poll = await _next(server)
    assert poll.args == {'after': 10, 'wait': 0.05}
    await _reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 12,
        'events': [
          {'seq': 11, 'kind': 'benchmark', 'quest': 'B1', 'transition': 'started'},
          {
            'seq': 12,
            'kind': 'summon',
            'quest': 'S1',
            'transition': 'denied',
            'reason': 'summon denied: not allowed',
          },
        ],
      },
    )
    assert await first_line == 'summon denied: not allowed (request S1)'

    second_line = asyncio.create_task(asyncio.to_thread(next, watch))
    channel, poll = await _next(server)
    assert poll.args == {'after': 12, 'wait': 0.05}
    await _reply(
      server,
      channel,
      poll,
      outcome='ok',
      value={
        'head': 13,
        'events': [
          {
            'seq': 13,
            'kind': 'summon',
            'quest': 'S1',
            'transition': 'ended',
            'outcome': 'failed',
            'reason': 'timeout',
          }
        ],
      },
    )
    assert await second_line == 'summon ended failed:timeout (request S1)'
    watch.close()


def test_may_summon_round_trips_the_launch_published_list(monkeypatch):
  monkeypatch.setenv(summon.MAY_SUMMON_ENV, summon.encode_may_summon({'reviewer', 'dev'}))
  assert summon.may_summon() == ('dev', 'reviewer')


def test_may_summon_distinguishes_empty_and_unpublished(monkeypatch):
  monkeypatch.setenv(summon.MAY_SUMMON_ENV, '')
  assert summon.may_summon() == ()
  monkeypatch.delenv(summon.MAY_SUMMON_ENV)
  assert summon.may_summon() is None


def test_errors_without_a_channel(monkeypatch, caplog):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert summon.main(['summon', 'dev', 'work']) == 1
  assert summon.main(['summon', 'check', 'SOME-ID']) == 1
  assert summon.main(['summon', 'list']) == 1
  assert CHANNEL_ENV in caplog.text
