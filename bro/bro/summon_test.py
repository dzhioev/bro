import asyncio
import contextlib
import json
import os
from dataclasses import dataclass

import pytest

from bro import summon
from broker.brotocol import Message
from broker.client import CHANNEL_ENV
from broker.transport import ChannelID
from broker.transports.unix import UnixServerTransport

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
async def running_server(socket_dir, monkeypatch):
  transport = UnixServerTransport(str(socket_dir))
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  provisioned = await transport.provision()
  monkeypatch.setenv(CHANNEL_ENV, 'unix:' + provisioned.host_endpoint)
  try:
    yield Harness(transport=transport, sink=sink)
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


async def _next(queue: asyncio.Queue):
  return await asyncio.wait_for(queue.get(), TIMEOUT)


def test_bare_summon_help_names_check_and_list(capsys):
  with pytest.raises(SystemExit):
    summon.main(['summon', '--help'])
  output = capsys.readouterr().out
  assert 'summon check' in output
  assert 'summon list' in output


def test_bare_summon_forwards_with_its_own_shell_command(monkeypatch):
  calls: list[tuple] = []
  monkeypatch.delenv('PPP_SHELL_COMMAND', raising=False)
  monkeypatch.setattr(
    summon,
    'relay_summon',
    lambda target, prompt, *, timeout, into, hold: (
      calls.append((target, prompt, timeout, into)) or 0
    ),
  )

  assert summon.main(['summon', '--timeout', '60', 'devoops', 'deploy']) == 0
  assert calls == [('devoops', 'deploy', 60.0, None)]
  assert os.environ['PPP_SHELL_COMMAND'] == 'summon --timeout 60.0 devoops deploy'


def test_errors_without_a_channel(monkeypatch, capsys, caplog):
  # unlike the substrate broker CLI, no channel is a failure, not inert
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert summon.main(['summon', 'devoops', 'deploy']) == 1
  assert summon.main(['summon', 'check', 'SOME-ID']) == 1
  assert capsys.readouterr().out == ''
  assert any(CHANNEL_ENV in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_blocking_summon_relays_the_answer(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    argv = ['summon', 'devoops', 'list the deploy targets']
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, argv))

    channel, request = await _next(server.sink.messages)
    assert request.type == 'summon'
    assert request.payload == {'target': 'devoops', 'prompt': 'list the deploy targets'}
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=request.id)
    )
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'alpha, beta', 'end_reason': 'ok'},
        in_reply_to=request.id,
      ),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    assert capsys.readouterr().out == 'alpha, beta\n'
    # the request id and the trail id both surfaced on stderr for reattach/inspection
    logged = [record.getMessage() for record in caplog.records]
    assert any(request.id in line for line in logged)
    assert any('T1' in line for line in logged)


@pytest.mark.asyncio
async def test_timeout_and_into_forward_into_the_request(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir, monkeypatch) as server:
    argv = ['summon', '--detach', '--timeout', '42', '--into', 'summon', 'devoops', 'p']
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, argv))

    _, request = await _next(server.sink.messages)
    assert request.payload == {
      'target': 'devoops',
      'prompt': 'p',
      'timeout': 42.0,
      'into': 'summon',
    }
    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    capsys.readouterr()


@pytest.mark.asyncio
async def test_hold_forwards_into_the_request(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir, monkeypatch) as server:
    argv = ['summon', '--detach', '--hold', 'attended', 'devoops', 'p']
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, argv))

    _, request = await _next(server.sink.messages)
    assert request.payload == {
      'target': 'devoops',
      'prompt': 'p',
      'hold': 'attended',
    }
    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    # --detach prints the request id as the data output and exits right away
    assert capsys.readouterr().out == f'{request.id}\n'


@pytest.mark.asyncio
async def test_raised_completion_is_a_failure(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'devoops', 'p']))

    channel, request = await _next(server.sink.messages)
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'missing credentials', 'end_reason': 'raised'},
        in_reply_to=request.id,
      ),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 1
    assert capsys.readouterr().out == ''
    assert any(
      'raised' in record.getMessage() and 'missing credentials' in record.getMessage()
      for record in caplog.records
    )


@pytest.mark.asyncio
async def test_failed_terminal_carries_a_trails_hint(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'devoops', 'p']))

    channel, request = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 'T9'}, in_reply_to=request.id)
    )
    await server.transport.send(
      channel,
      Message(type='failed', payload={'reason': 'timeout'}, in_reply_to=request.id),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 1
    assert capsys.readouterr().out == ''
    assert any(
      'failed (timeout)' in record.getMessage() and 'trails show T9' in record.getMessage()
      for record in caplog.records
    )


@pytest.mark.asyncio
async def test_denial_reply_is_a_failure(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'pm', 'p']))

    channel, request = await _next(server.sink.messages)
    await server.transport.send(
      channel,
      Message(
        type='reply',
        payload={'error': "summon denied: 'pm' is not in this session's summon allow-list"},
        in_reply_to=request.id,
      ),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 1
    assert capsys.readouterr().out == ''
    assert any('summon denied' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_check_wait_claims_and_relays_the_buffered_answer(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'check', '--wait', 'REQ-1'])
    )

    # the broxy would replay buffered messages re-tagged to the claim itself;
    # this stub host answers the claim the same way
    channel, claim = await _next(server.sink.messages)
    assert claim.type == 'claim'
    assert claim.payload == {'id': 'REQ-1'}
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=claim.id)
    )
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'late answer', 'end_reason': 'ok'},
        in_reply_to=claim.id,
      ),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    assert capsys.readouterr().out == 'late answer\n'


@pytest.mark.asyncio
async def test_check_wait_unknown_claim_fails_fast(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'check', '--wait', 'NOPE'])
    )

    channel, claim = await _next(server.sink.messages)
    await server.transport.send(
      channel,
      Message(
        type='reply',
        payload={'error': 'unknown request id NOPE (not sent through this session, or evicted)'},
        in_reply_to=claim.id,
      ),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 1
    assert capsys.readouterr().out == ''
    assert any('unknown request id' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_check_relays_a_ready_answer(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))

    # the broxy replays a buffered terminal re-tagged to the check itself;
    # this stub host answers the check the same way
    channel, check = await _next(server.sink.messages)
    assert check.type == 'check'
    assert check.payload == {'id': 'REQ-1'}
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=check.id)
    )
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'ready answer', 'end_reason': 'ok'},
        in_reply_to=check.id,
      ),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    assert capsys.readouterr().out == 'ready answer\n'


@pytest.mark.asyncio
async def test_check_pending_exits_3_without_blocking(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))

    channel, check = await _next(server.sink.messages)
    await server.transport.send(
      channel,
      Message(type='reply', payload={'state': 'pending', 'trail_id': 'T9'}, in_reply_to=check.id),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == summon.PENDING_EXIT_CODE
    assert capsys.readouterr().out == ''
    assert any(
      'still running' in record.getMessage() and 'T9' in record.getMessage()
      for record in caplog.records
    )


@pytest.mark.asyncio
async def test_check_unknown_id_exits_1(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'NOPE']))

    channel, check = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='reply', payload={'state': 'unknown'}, in_reply_to=check.id)
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 1
    assert capsys.readouterr().out == ''
    assert any('unknown request id NOPE' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_check_last_seen_forwards_the_cursor_and_relays(socket_dir, monkeypatch, capsys):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(
      asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1', '--last-seen', '0'])
    )

    # the broxy replays the whole conversation re-tagged to the cursor check
    channel, check = await _next(server.sink.messages)
    assert check.type == 'check'
    assert check.payload == {'id': 'REQ-1', 'last_seen': 0}
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 'T1'}, in_reply_to=check.id)
    )
    await server.transport.send(
      channel,
      Message(
        type='completed',
        payload={'result': 'recovered answer', 'end_reason': 'ok'},
        in_reply_to=check.id,
      ),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 0
    assert capsys.readouterr().out == 'recovered answer\n'


@pytest.mark.asyncio
async def test_check_collected_reports_the_cursor_hint(socket_dir, monkeypatch, capsys, caplog):
  async with running_server(socket_dir, monkeypatch) as server:
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']))

    channel, check = await _next(server.sink.messages)
    await server.transport.send(
      channel,
      Message(type='reply', payload={'state': 'collected', 'seq': 2}, in_reply_to=check.id),
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 1
    assert capsys.readouterr().out == ''
    messages = [
      record.getMessage() for record in caplog.records if 'already read' in record.getMessage()
    ]
    assert len(messages) == 1
    assert 'seq 2' in messages[0]
    assert '--last-seen 0' in messages[0]


def test_check_last_seen_with_wait_errors(monkeypatch, capsys, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'unix:/nonexistent')
  argv = ['summon', 'check', 'REQ-1', '--wait', '--last-seen', '0']
  assert summon.main(argv) == 1
  assert capsys.readouterr().out == ''
  assert any('--wait' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_check_that_nothing_answers_fails_with_a_hint(
  socket_dir, monkeypatch, capsys, caplog
):
  # a channel with no broxy behind it never answers a check — rule 4 refuses
  # silently — so the bounded wait must turn that silence into a clean failure
  # naming the broxy
  async with running_server(socket_dir, monkeypatch):
    monkeypatch.setattr(summon, 'CHECK_TIMEOUT', 0.1)
    assert await asyncio.to_thread(summon.main, ['summon', 'check', 'REQ-1']) == 1
    assert capsys.readouterr().out == ''
    assert any('broxy' in record.getMessage() for record in caplog.records)


def test_list_errors_without_a_status_file_env(monkeypatch, capsys, caplog):
  monkeypatch.delenv(summon.STATUS_ENV, raising=False)
  assert summon.main(['summon', 'list']) == 1
  assert capsys.readouterr().out == ''
  assert any(summon.STATUS_ENV in record.getMessage() for record in caplog.records)


def test_list_reports_empty_before_any_summon(tmp_path, monkeypatch, capsys):
  # the host writes the status file with the session's first summon; before that
  # the pointed-at path does not exist and the state is simply empty
  monkeypatch.setenv(summon.STATUS_ENV, str(tmp_path / 'ws.status.json'))
  assert summon.main(['summon', 'list']) == 0
  assert json.loads(capsys.readouterr().out) == {'active': [], 'last': None}


def test_list_prints_the_recorded_status(tmp_path, monkeypatch, capsys):
  status = {
    'active': [{'request_id': 'R1', 'target': 'devoops', 'trail_id': 'T1', 'started_at': 1.0}],
    'last': {'request_id': 'R0', 'target': 'pm', 'outcome': 'ok'},
  }
  status_file = tmp_path / 'ws.status.json'
  status_file.write_text(json.dumps(status))
  monkeypatch.setenv(summon.STATUS_ENV, str(status_file))
  assert summon.main(['summon', 'list']) == 0
  assert json.loads(capsys.readouterr().out) == status


def test_check_timeout_without_wait_errors(monkeypatch, capsys, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'unix:/nonexistent')
  assert summon.main(['summon', 'check', 'REQ-1', '--timeout', '5']) == 1
  assert capsys.readouterr().out == ''
  assert any('--wait' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_wait_after_started_is_bounded_with_a_trails_hint(
  socket_dir, monkeypatch, capsys, caplog
):
  # started re-arms the wait to the effective timeout (down from the launch
  # backstop), so a lost terminal becomes a clean failure with a trails hint
  async with running_server(socket_dir, monkeypatch) as server:
    argv = ['summon', '--timeout', '0.1', 'devoops', 'p']
    main_task = asyncio.create_task(asyncio.to_thread(summon.main, argv))

    channel, request = await _next(server.sink.messages)
    await server.transport.send(
      channel, Message(type='started', payload={'trail_id': 'T7'}, in_reply_to=request.id)
    )

    assert await asyncio.wait_for(main_task, TIMEOUT) == 1
    assert capsys.readouterr().out == ''
    assert any(
      'no summon terminal' in record.getMessage() and 'trails show T7' in record.getMessage()
      for record in caplog.records
    )


@pytest.mark.asyncio
async def test_prestarted_expiry_points_at_the_summon_status(
  socket_dir, monkeypatch, capsys, caplog
):
  # nothing correlated ever arrives: the wait expires at the launch backstop, and
  # the message points at the summon status/audit — a trails hint is a dead end
  # (no trail exists for a child that never launched)
  async with running_server(socket_dir, monkeypatch):
    monkeypatch.setattr(summon, 'LAUNCH_TIMEOUT', 0.1)
    argv = ['summon', '--timeout', '0.05', 'devoops', 'p']
    assert await asyncio.to_thread(summon.main, argv) == 1
    assert capsys.readouterr().out == ''
    messages = [
      record.getMessage() for record in caplog.records if 'never launched' in record.getMessage()
    ]
    assert len(messages) == 1
    assert 'var/cw/summon' in messages[0]
    assert 'trails' not in messages[0]
