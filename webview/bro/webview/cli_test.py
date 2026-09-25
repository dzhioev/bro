import asyncio
import json

import pytest

from bro import mission
from bro.broker import brotocol
from bro.broker.brotocol import Tag
from bro.quest_test_helper import next_message, quest_record, reply, running_server
from bro.webview import cli

MISSION = 'WEB-1'
REF = 'sha256:' + 'a' * 64


def _live_webview(**overrides):
  ready = {
    'seq': 1,
    'at': 'now',
    'transition': 'message',
    'from': 'worker',
    'head': {'event': 'ready', 'vnc': None},
  }
  values = {'messages': [ready], 'chat_seq': 1, **overrides}
  return quest_record(
    MISSION,
    'started',
    type='webview',
    talk=['owner.question', 'worker.say'],
    **values,
  )


async def _complete_close(server, *, start_ready: bool = True) -> None:
  channel, current = await next_message(server)
  first = _live_webview() if start_ready else _live_webview(messages=[], chat_seq=0)
  await reply(server, channel, current, outcome='ok', value={'mission': first})
  if not start_ready:
    channel, readiness = await next_message(server)
    assert readiness.args == {'id': MISSION, 'wait': mission.READ_WAIT_SECONDS, 'since': 0}
    await reply(server, channel, readiness, outcome='ok', value={'mission': _live_webview()})
  channel, validation = await next_message(server)
  await reply(server, channel, validation, outcome='ok', value={'mission': _live_webview()})
  _, question = await next_message(server)
  assert question.type == Tag.MESSAGE
  assert question.request_id == MISSION
  assert question.payload == {'webview': 'close'}
  assert question.id is not None
  asked = {
    'seq': 1,
    'at': 'now',
    'transition': 'message',
    'from': 'owner',
    'head': {'webview': 'close'},
    'id': question.id,
    'pending': True,
  }
  answered = {
    'seq': 2,
    'at': 'now',
    'transition': 'message',
    'from': 'worker',
    'head': {'closed': True},
    'reply_to': question.id,
  }
  channel, conversation = await next_message(server)
  await reply(
    server,
    channel,
    conversation,
    outcome='ok',
    value={'mission': _live_webview(messages=[asked, answered], chat_seq=2)},
  )
  channel, terminal = await next_message(server)
  assert terminal.args == {
    'id': MISSION,
    'wait': mission.READ_WAIT_SECONDS,
    'settled': True,
  }
  await reply(
    server,
    channel,
    terminal,
    outcome='ok',
    value={
      'mission': quest_record(
        MISSION,
        'ended',
        type='webview',
        result={'outcome': 'ok', 'value': {'commands': 7}},
        settled=True,
      )
    },
  )


@pytest.mark.asyncio
async def test_open_rides_the_launch_to_ready_and_prints_the_owner_handle(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(
      asyncio.to_thread(
        cli.main,
        [
          'webview',
          'open',
          '--vnc',
          '--allow',
          'https://allowed.example',
          '--block',
          'https://blocked.example',
          '--share',
          REF,
          '--timeout',
          '90',
        ],
      )
    )
    channel, launch = await next_message(server)
    assert launch.kind == 'launch'
    assert launch.args == {
      'type': 'webview',
      'vnc': True,
      'allowed_origins': ['https://allowed.example'],
      'blocked_origins': ['https://blocked.example'],
      'share': [REF],
      'timeout': 90.0,
    }
    await server.transport.send(channel, brotocol.mark(launch.request_id, 'accepted'))
    await server.transport.send(channel, brotocol.mark(launch.request_id, 'started'))
    await server.transport.send(channel, brotocol.mark(launch.request_id, 'listening'))
    await server.transport.send(
      channel,
      brotocol.message(
        launch.request_id,
        {
          'event': 'ready',
          'vnc': 'http://127.0.0.1:49152/vnc.html?autoconnect=1&resize=scale',
        },
      ),
    )

    assert await task == 0
    assert json.loads(capsys.readouterr().out) == {
      'mission': launch.request_id,
      'vnc': 'http://127.0.0.1:49152/vnc.html?autoconnect=1&resize=scale',
    }


@pytest.mark.asyncio
async def test_open_fails_with_a_launch_denial(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(cli.main, ['webview', 'open', '--vnc']))
    channel, launch = await next_message(server)
    await server.transport.send(
      channel,
      brotocol.result(
        launch.request_id,
        'denied',
        error='launch denied: the VNC view needs :webview.vnc',
      ),
    )

    assert await task == 1
    assert capsys.readouterr().out == ''
    assert ':webview.vnc' in caplog.text


@pytest.mark.asyncio
async def test_open_cancels_its_launch_when_startup_times_out(monkeypatch, caplog):
  monkeypatch.setattr(cli, 'OPEN_TIMEOUT', 0.01)
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(cli.main, ['webview', 'open']))
    _, launch = await next_message(server)
    channel, cancel = await next_message(server)
    assert cancel.kind == 'cancel'
    assert cancel.args == {'id': launch.request_id}
    await reply(server, channel, cancel, outcome='ok')

    assert await task == 1
    assert 'timed out' in caplog.text
    assert 'cancelled' in caplog.text


@pytest.mark.asyncio
async def test_open_cancels_a_live_launch_when_ready_is_malformed(monkeypatch, caplog):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(cli.main, ['webview', 'open']))
    channel, launch = await next_message(server)
    await server.transport.send(channel, brotocol.mark(launch.request_id, 'accepted'))
    await server.transport.send(channel, brotocol.mark(launch.request_id, 'started'))
    await server.transport.send(
      channel,
      brotocol.message(
        launch.request_id,
        {'event': 'ready', 'vnc': None, 'unexpected': True},
      ),
    )
    channel, cancel = await next_message(server)
    assert cancel.kind == 'cancel'
    assert cancel.args == {'id': launch.request_id}
    await reply(server, channel, cancel, outcome='ok')

    assert await task == 1
    assert 'ready event is malformed' in caplog.text


@pytest.mark.asyncio
async def test_close_waits_for_the_reply_and_terminal_outcome(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(cli.main, ['webview', 'close', MISSION]))
    await _complete_close(server)

    assert await task == 0
    assert json.loads(capsys.readouterr().out) == {
      'outcome': 'ok',
      'value': {'commands': 7},
    }


@pytest.mark.asyncio
async def test_close_waits_for_the_ready_say_before_sending_its_question(monkeypatch, capsys):
  async with running_server(monkeypatch) as server:
    task = asyncio.create_task(asyncio.to_thread(cli.main, ['webview', 'close', MISSION]))
    await _complete_close(server, start_ready=False)

    assert await task == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'ok'


def test_close_without_a_mission_is_a_usage_error(capsys):
  with pytest.raises(SystemExit) as exited:
    cli.main(['webview', 'close'])

  assert exited.value.code == 2
  assert 'MISSION' in capsys.readouterr().err


def test_close_without_a_channel_fails_before_sending_a_command(monkeypatch, caplog):
  monkeypatch.setenv('BROKER_MISSION', 'ROOT')
  monkeypatch.delenv('BROKER_CHANNEL', raising=False)

  assert cli.main(['webview', 'close', MISSION]) == 1
  assert 'no broker channel' in caplog.text


def test_close_reports_a_failed_session_proxy(monkeypatch, caplog):
  monkeypatch.setenv('BROKER_MISSION', 'ROOT')
  monkeypatch.setenv('BROKER_UPSTREAM', 'tcp://token@127.0.0.1:1')
  monkeypatch.delenv('BROKER_CHANNEL', raising=False)

  assert cli.main(['webview', 'close', MISSION]) == 1
  assert 'session proxy failed at launch' in caplog.text


def test_missing_verb_is_a_usage_error(capsys):
  assert cli.main(['webview']) == 1
  assert 'usage' in capsys.readouterr().err
